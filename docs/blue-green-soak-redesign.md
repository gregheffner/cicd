> Status: APPROVED direction, STAGED for implementation after Phase 0 (the OpenSSL patch + Trivy gate, committed in this branch).
> Decisions: weekly build + 3-day soak; auto-promote on green gates; replace the 4 calendar workflows with 2 state-aware ones.
> Generated + adversarially verified by multi-agent workflow on 2026-06-20. Implement P1-P8 cluster prereqs (Section 2) BEFORE enabling crons.

# FINAL DESIGN — gregheffner/cicd blue/green build → 3-day soak → auto-promote

## 1. Executive summary

Two state-aware GitHub Actions workflows replace four calendar-driven ones. **build-stage-scan** runs weekly (Mon 07:00 UTC): on an ephemeral `ubuntu-latest` runner it generates a Dockerfile from a **digest-pinned** base, builds **single-arch linux/amd64**, runs a Trivy HIGH/CRITICAL **gate before any push**, pushes `:latest`+`:vYYYY.MM.DD`, signs with cosign, captures the digest, then on the `self-hosted` runner deploys that digest onto the **standby** color (the non-live color, derived dynamically from the Service selector) under a locked-down soak identity and waits for a real **readiness-probe-backed** Ready. **soak-gate-promote** runs daily (07:30 UTC) and promotes only when ALL gates pass: soak ≥72h (epoch-integer clock), fresh-DB Trivy re-scan of the exact soaked digest clean, cosign verify, base-digest unchanged, ≥1 Ready endpoint on the target, running image == soaked digest, and the incoming color is an autoscaling Deployment with an HPA. It then does an **atomic Service-selector flip** with a **fencing re-read** of the ledger immediately before the irreversible patch, smoke-tests through the LB, and auto-rolls-back on failure. State lives in a committed `.github/state/candidate.json` ledger with a compare-and-abort fence so a build landing mid-promote can never silently reset the soak clock. **No-outage** rests on: both colors symmetric Deployments with readiness probes + PDB + preStop drain, `externalTrafficPolicy: Cluster`, the old color never scaled down before the flip, and the Saturday pod-delete cron scoped out of prod. Discarding an un-promoted soaking candidate is a **hard failure** (loud, not silent), so the live image always advances.

---

## 2. HARD PREREQUISITES (must land in this order, before either cron is enabled)

These resolve the topology/capacity/autoscaling/traffic blockers. They are **not optional**.

**P1 — Confirm fail2ban bans on `CF-Connecting-IP`, then set `externalTrafficPolicy: Cluster`.** With Cloudflare terminating TLS and fronting the origin, the real client IP is in the `CF-Connecting-IP` header; the L4 source becomes Cloudflare/SNAT. `Cluster` policy is what makes a flip genuinely zero-drop (any node can route to any endpoint). **Verify the fail2ban `cloudflare-ban` filter keys on `CF-Connecting-IP` (or `X-Forwarded-For`), not the TCP source, before this change** — otherwise IP bans break. (This is config the operator inspects in the `nginx-config`/`fail2ban-config` configmaps.)

**P2 — Convert `DR/nginx-green.yaml` from DaemonSet to a Deployment** mirroring blue (`replicas: 3`), so both colors are autoscaling-capable, symmetric live peers. This single change fixes three blockers: DaemonSet-can't-have-HPA, asymmetric-Local-traffic-drop, and capacity asymmetry.

**P3 — Add a real `readinessProbe` to the nginx container in BOTH colors** (`httpGet / :80`). Without it, "Ready" means "process started", not "serving 200s", and every gate is hollow.

**P4 — Add a PodDisruptionBudget + `terminationGracePeriodSeconds` + nginx `preStop` drain to both colors**, and **scope the Saturday delete-pods cron out of prod** (P7), so bulk/rolling pod churn rolls instead of blackholing.

**P5 — Fix the HPA metric.** `AverageValue: 3000m` against `requests.cpu: 100m` never fires. Switch to `Utilization` at 70% (relative to a realistic request) and raise `requests.cpu` to something representative (`250m`).

**P6 — Create a minimal-RBAC soak ServiceAccount (`nginx-soak`)** with no permissions, used by the standby pods while soaking, with `automountServiceAccountToken: false`. (On flannel, NetworkPolicy is unenforced — token de-mounting + ephemeral build runner are the real isolation; the NetworkPolicy is best-effort and documented as such.)

**P7 — Put `delete-kubernetes-pods.yaml` in the shared concurrency group AND drop `prod` from its target list**, so it can never bounce a live or soaking color mid-gate.

**P8 — Verify the action SHAs and the self-hosted runner toolchain.** Run the operator-verification block in §11 step 0. The ledger stores the soak clock as an **epoch integer** so promote does only integer comparison (no GNU-`date` dependency on the self-hosted runner).

---

## 3. `.github/workflows/build-stage-scan.yaml`

```yaml
name: Build, Stage & Scan Candidate

on:
  workflow_dispatch:
  schedule:
    # Weekly Mondays 07:00 UTC -> 72h soak completes Thu 07:00 -> promoted Thu 07:30.
    - cron: '0 7 * * 1'

concurrency:
  group: nginx-pipeline           # shared with soak-gate-promote AND delete-pods
  cancel-in-progress: false

permissions:
  contents: write                 # commit candidate.json + standby manifest
  id-token: write                 # cosign keyless OIDC signing

jobs:
  # ---------------------------------------------------------------------------
  # JOB 1 — build on EPHEMERAL ubuntu-latest. NO kubeconfig here on purpose:
  # apk runs pre/post-install + trigger scripts as ROOT at build time, so the
  # build must never touch the cluster-credentialed self-hosted runner.
  # ---------------------------------------------------------------------------
  build:
    runs-on: ubuntu-latest
    outputs:
      immutable_tag: ${{ steps.meta.outputs.tag }}
      digest:        ${{ steps.digest.outputs.digest }}
      base_digest:   ${{ steps.basedigest.outputs.base }}
      standby_color: ${{ steps.color.outputs.standby }}
      standby_obj:   ${{ steps.color.outputs.obj }}
      live_color:    ${{ steps.color.outputs.live }}
    steps:
      - name: Checkout
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2 (verify in §11.0)

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@b5ca514318bd6ebac0fb2aedd5d36ec1b5c232a2  # v3.10.0 (verify)

      - name: Set up Python
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0 (verify)
        with:
          python-version: '3.x'

      - name: Install generator deps
        run: pip install requests packaging

      - name: Install cosign
        uses: sigstore/cosign-installer@d7d6bc7722e3daa8354c50bcb52f4837da5e9b6a  # v3.7.0 (verify)

      - name: Generate Dockerfile (digest-pinned base, apk upgrade, COPY-before-chmod, stable branch)
        run: python .github/scripts/generate_dockerfile.py

      - name: Show generated Dockerfile
        run: cat Dockerfile

      - name: Capture base image digest (from generated Dockerfile FROM line)
        id: basedigest
        run: |
          set -euo pipefail
          BASE=$(grep -m1 '^FROM ' Dockerfile | awk '{print $2}')
          case "$BASE" in
            *@sha256:*) ;;
            *) echo "::error::Base image is not digest-pinned: $BASE"; exit 1 ;;
          esac
          echo "base=$BASE" >> "$GITHUB_OUTPUT"
          echo "Base pinned to $BASE"

      - name: Compute immutable date tag
        id: meta
        run: echo "tag=v$(date -u +'%Y.%m.%d')" >> "$GITHUB_OUTPUT"

      - name: Log in to Docker Hub
        uses: docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772  # v3.4.0 (verify)
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      # Build & LOAD locally (single-arch so list-digest == image-digest). Scan BEFORE push.
      - name: Build image (linux/amd64, load locally, do NOT push)
        uses: docker/build-push-action@471d1dc4e07e5cdedd4c2171150001c434f0b7a4  # v6.15.0 (verify)
        with:
          context: .
          platforms: linux/amd64        # single-arch: cluster is one arch; keeps digest semantics simple
          load: true
          push: false
          tags: technotuba/nginx:candidate
          provenance: false

      - name: Trivy report (informational, includes unfixed)
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25  # v0.36.0 (verify behavior, §11.0)
        with:
          image-ref: technotuba/nginx:candidate
          scan-type: image
          scanners: vuln
          vuln-type: os,library
          severity: 'HIGH,CRITICAL'
          ignore-unfixed: false
          format: table
          exit-code: '0'

      # GATE — the real fail-before-push. Separate step; format:table keeps exit-code live.
      # No trivyignores: there are no CVEs to suppress (OpenSSL fix is via apk upgrade, not ignore).
      # ignore-unfixed:true means unfixable noise never blocks; a fixable HIGH/CRITICAL DOES block.
      - name: Trivy GATE (fail on fixable HIGH,CRITICAL — blocks push)
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25  # v0.36.0
        with:
          image-ref: technotuba/nginx:candidate
          scan-type: image
          scanners: vuln
          vuln-type: os,library
          severity: 'HIGH,CRITICAL'
          ignore-unfixed: true
          format: table
          exit-code: '1'

      - name: Tag and push (latest + immutable)
        run: |
          set -euo pipefail
          TAG="${{ steps.meta.outputs.tag }}"
          docker tag technotuba/nginx:candidate technotuba/nginx:latest
          docker tag technotuba/nginx:candidate "technotuba/nginx:${TAG}"
          docker push "technotuba/nginx:${TAG}"
          docker push technotuba/nginx:latest

      - name: Capture immutable digest
        id: digest
        run: |
          set -euo pipefail
          TAG="${{ steps.meta.outputs.tag }}"
          DIGEST=$(docker buildx imagetools inspect "technotuba/nginx:${TAG}" \
            --format '{{json .Manifest.Digest}}' | tr -d '"')
          case "$DIGEST" in
            sha256:*) ;;
            *) echo "::error::Failed to resolve digest for ${TAG} (got '${DIGEST}')"; exit 1 ;;
          esac
          echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"
          echo "Resolved ${TAG} -> ${DIGEST}"

      - name: Cosign sign the exact digest (keyless OIDC)
        env:
          COSIGN_YES: "true"
        run: |
          set -euo pipefail
          cosign sign "technotuba/nginx@${{ steps.digest.outputs.digest }}"

      - name: Install yq
        run: |
          set -euo pipefail
          sudo wget -qO /usr/local/bin/yq \
            https://github.com/mikefarah/yq/releases/download/v4.45.1/yq_linux_amd64
          sudo chmod +x /usr/local/bin/yq
          yq --version

      # Resolve STANDBY from the LIVE Service selector via the committed manifest (GitOps SoT).
      # Document-scoped read (Service doc only) — the file is multi-doc until P-split lands.
      - name: Resolve live/standby color from committed Service selector
        id: color
        run: |
          set -euo pipefail
          LIVE=$(yq '(select(.kind == "Service") | .spec.selector.version)' shared/nginx-service.yaml | head -n1)
          case "$LIVE" in
            blue)  STANDBY=green; OBJ="deployment/nginx-web-green"; FILE=DR/nginx-green.yaml ;;
            green) STANDBY=blue;  OBJ="deployment/nginx-web-blue";  FILE=prod/nginx-blue.yaml ;;
            *) echo "::error::Service selector version is '$LIVE' (expected blue|green). Aborting."; exit 1 ;;
          esac
          {
            echo "live=$LIVE"
            echo "standby=$STANDBY"
            echo "obj=$OBJ"
            echo "file=$FILE"
          } >> "$GITHUB_OUTPUT"
          echo "LIVE=$LIVE  ->  STANDBY=$STANDBY ($FILE)"

      # Pin the STANDBY manifest image to the immutable DIGEST via sed (preserves the
      # file's leading-2-space indentation -> surgical one-line diff, not a yq reflow).
      # Matches only the technotuba/nginx line; leaves crazymax/fail2ban untouched.
      - name: Pin standby manifest image to digest (sed, surgical)
        run: |
          set -euo pipefail
          FILE="${{ steps.color.outputs.file }}"
          DIGEST="${{ steps.digest.outputs.digest }}"
          sed -i -E 's#(image: )technotuba/nginx[:@][^[:space:]"]*#\1technotuba/nginx@'"$DIGEST"'#' "$FILE"
          echo "Pinned $FILE nginx image -> technotuba/nginx@$DIGEST"
          grep -n 'technotuba/nginx' "$FILE"
          grep -q "technotuba/nginx@${DIGEST}" "$FILE" || { echo "::error::pin failed"; exit 1; }

      # Fail-loud overwrite guard: if a prior candidate is still soaking, REFUSE
      # unless this build's digest differs AND the prior is genuinely superseded.
      # Idempotent rebuild (same digest) preserves the clock; a NEW digest while the
      # old one is un-promoted is a HARD ERROR (liveness: never silently reset soak).
      - name: Overwrite guard (fail loud on unparseable or supersede)
        id: guard
        run: |
          set -euo pipefail
          F=.github/state/candidate.json
          if [ ! -f "$F" ]; then echo "no prior candidate"; echo "preserve_clock=false" >> "$GITHUB_OUTPUT"; exit 0; fi
          # Parse-or-abort: do NOT swallow corruption.
          PREV_STATE=$(yq -p=json -e '.state' "$F") || { echo "::error::candidate.json unparseable; refusing to overwrite."; exit 1; }
          PREV_DIGEST=$(yq -p=json -e '.digest' "$F") || { echo "::error::candidate.json unparseable; refusing to overwrite."; exit 1; }
          NEW_DIGEST="${{ steps.digest.outputs.digest }}"
          if [ "$PREV_STATE" = "soaking" ]; then
            if [ "$PREV_DIGEST" = "$NEW_DIGEST" ]; then
              echo "Idempotent rebuild: same digest as soaking candidate; preserving existing soak clock."
              echo "preserve_clock=true" >> "$GITHUB_OUTPUT"
            else
              echo "::error::A DIFFERENT candidate ($PREV_DIGEST) is still soaking and un-promoted."
              echo "::error::Building $NEW_DIGEST would discard its soak. Promote or explicitly abandon it first."
              echo "::error::To intentionally supersede: delete .github/state/candidate.json (or set state!=soaking) and re-run."
              exit 1
            fi
          else
            echo "preserve_clock=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Write candidate.json (state ledger; epoch soak clock)
        run: |
          set -euo pipefail
          mkdir -p .github/state
          F=.github/state/candidate.json
          NOW_EPOCH=$(date -u +%s)
          NOW_ISO=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
          if [ "${{ steps.guard.outputs.preserve_clock }}" = "true" ]; then
            BUILD_EPOCH=$(yq -p=json -e '.build_epoch' "$F")
            ELIG_EPOCH=$(yq -p=json -e '.promote_eligible_after_epoch' "$F")
            BUILD_ISO=$(yq -p=json -e '.build_timestamp_utc' "$F")
          else
            BUILD_EPOCH="$NOW_EPOCH"
            ELIG_EPOCH=$(( NOW_EPOCH + 259200 ))   # +72h
            BUILD_ISO="$NOW_ISO"
          fi
          TAG="${{ steps.meta.outputs.tag }}"
          DIGEST="${{ steps.digest.outputs.digest }}"
          BASE="${{ steps.basedigest.outputs.base }}"
          STANDBY="${{ steps.color.outputs.standby }}"
          OBJ="${{ steps.color.outputs.obj }}"
          LIVE="${{ steps.color.outputs.live }}"
          RUN_URL="${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
          cat > "$F" <<EOF
          {
            "schema": 2,
            "immutable_tag": "${TAG}",
            "digest": "${DIGEST}",
            "image_ref": "technotuba/nginx@${DIGEST}",
            "base_image": "${BASE}",
            "standby_color": "${STANDBY}",
            "standby_object": "${OBJ}",
            "live_color_at_build": "${LIVE}",
            "build_timestamp_utc": "${BUILD_ISO}",
            "build_epoch": ${BUILD_EPOCH},
            "build_scan_status": "pass",
            "promote_eligible_after_epoch": ${ELIG_EPOCH},
            "state": "soaking",
            "promoted_timestamp_utc": null,
            "run_url": "${RUN_URL}"
          }
          EOF
          cat "$F"

      - name: Commit candidate.json + standby manifest (fetch+rebase+retry)
        run: |
          set -euo pipefail
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git config user.name  "github-actions[bot]"
          git add .github/state/candidate.json "${{ steps.color.outputs.file }}"
          if git diff --cached --quiet; then echo "no changes to commit"; exit 0; fi
          git commit -m "ci(stage): candidate ${{ steps.meta.outputs.tag }} -> ${{ steps.color.outputs.standby }} (soaking) [skip ci]"
          for i in 1 2 3 4 5; do
            git fetch origin "${{ github.ref_name }}"
            git rebase "origin/${{ github.ref_name }}" && git push origin "HEAD:${{ github.ref_name }}" && { echo pushed; exit 0; }
            echo "push attempt $i failed; retrying"; sleep $((i*3))
          done
          echo "::error::Failed to push candidate after retries"; exit 1

  # ---------------------------------------------------------------------------
  # JOB 2 — deploy standby on SELF-HOSTED (kubectl). No build/apk surface here.
  # ---------------------------------------------------------------------------
  deploy-standby:
    needs: build
    runs-on: self-hosted
    steps:
      - name: Checkout (pull just-pushed standby manifest)
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          ref: ${{ github.ref_name }}

      # Fence: the ledger must still hold THIS run's digest before we touch the cluster.
      - name: Fence — ledger still holds this run's digest
        run: |
          set -euo pipefail
          F=.github/state/candidate.json
          if ! command -v yq >/dev/null 2>&1; then
            sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/download/v4.45.1/yq_linux_amd64
            sudo chmod +x /usr/local/bin/yq
          fi
          LEDGER_DIGEST=$(yq -p=json -e '.digest' "$F")
          WANT="${{ needs.build.outputs.digest }}"
          [ "$LEDGER_DIGEST" = "$WANT" ] || { echo "::error::Ledger digest $LEDGER_DIGEST != this run $WANT; another build superseded us. Aborting."; exit 1; }

      - name: Re-confirm live color against the LIVE cluster
        run: |
          set -euo pipefail
          LIVE=$(kubectl get svc nginx-service -n prod -o jsonpath='{.spec.selector.version}')
          EXPECT="${{ needs.build.outputs.live_color }}"
          [ "$LIVE" = "$EXPECT" ] || { echo "::error::Live drifted: cluster=$LIVE build-assumed=$EXPECT. Aborting."; exit 1; }
          echo "Confirmed live=$LIVE, deploying standby=${{ needs.build.outputs.standby_color }}"

      - name: Apply standby manifest (digest-pinned)
        run: |
          set -euo pipefail
          case "${{ needs.build.outputs.standby_color }}" in
            blue)  kubectl apply -f prod/nginx-blue.yaml ;;
            green) kubectl apply -f DR/nginx-green.yaml ;;
            *) echo "::error::unknown standby color"; exit 1 ;;
          esac

      - name: Wait for standby rollout Ready
        run: |
          set -euo pipefail
          kubectl rollout status "${{ needs.build.outputs.standby_obj }}" -n prod --timeout=300s

      - name: Verify standby actually serving (readiness-probe-backed)
        run: |
          set -euo pipefail
          kubectl wait --for=condition=ready pod \
            -l app=nginx-web,version=${{ needs.build.outputs.standby_color }} \
            -n prod --timeout=120s

      # Best-effort egress lockdown. NOTE: cluster CNI is flannel -> NetworkPolicy is
      # NOT enforced; this is documentation/defense-in-depth only. Real soak isolation
      # is automountServiceAccountToken:false + minimal SA (in the manifest) + the
      # ephemeral build runner. See RESIDUAL RISKS.
      - name: Apply default-deny-egress NetworkPolicy to standby (best-effort on flannel)
        run: |
          set -euo pipefail
          kubectl apply -n prod -f - <<EOF
          apiVersion: networking.k8s.io/v1
          kind: NetworkPolicy
          metadata:
            name: soak-deny-egress-${{ needs.build.outputs.standby_color }}
            namespace: prod
          spec:
            podSelector:
              matchLabels:
                app: nginx-web
                version: ${{ needs.build.outputs.standby_color }}
            policyTypes: [Egress]
            egress:
              - to: []
                ports:
                  - { protocol: UDP, port: 53 }
                  - { protocol: TCP, port: 53 }
          EOF
```

---

## 4. `.github/workflows/soak-gate-promote.yaml`

```yaml
name: Soak Gate & Promote

on:
  workflow_dispatch:
  schedule:
    - cron: '30 7 * * *'          # daily 07:30 UTC

concurrency:
  group: nginx-pipeline           # shared with build-stage-scan AND delete-pods
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  gate-and-promote:
    runs-on: self-hosted          # needs kubectl + docker (re-scan) + cosign
    steps:
      - name: Checkout
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          ref: ${{ github.ref_name }}

      - name: Install yq + cosign (idempotent)
        run: |
          set -euo pipefail
          if ! command -v yq >/dev/null 2>&1; then
            sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/download/v4.45.1/yq_linux_amd64
            sudo chmod +x /usr/local/bin/yq
          fi
          yq --version
          command -v cosign >/dev/null 2>&1 || { echo "::error::cosign not installed on self-hosted runner (see §11.0)"; exit 1; }

      # ----- Read state. Exit clean if nothing to do. Capture digest for fencing. -----
      - name: Read candidate.json
        id: cand
        run: |
          set -euo pipefail
          F=.github/state/candidate.json
          if [ ! -f "$F" ]; then echo "No candidate.json — nothing to promote."; echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; fi
          SCHEMA=$(yq -p=json -e '.schema' "$F") || { echo "::error::candidate.json unparseable"; exit 1; }
          [ "$SCHEMA" = "2" ] || { echo "::error::Unexpected schema $SCHEMA (want 2)"; exit 1; }
          STATE=$(yq -p=json -e '.state' "$F")
          COLOR=$(yq -p=json -e '.standby_color' "$F")
          # Reconcile path: cluster already flipped but ledger stuck at soaking (partial-promote).
          if [ "$STATE" = "soaking" ]; then
            LIVE=$(kubectl get svc nginx-service -n prod -o jsonpath='{.spec.selector.version}')
            if [ "$LIVE" = "$COLOR" ]; then
              echo "::warning::Cluster live=$COLOR already == candidate color but ledger=soaking. Reconciling -> promoted."
              echo "reconcile=true" >> "$GITHUB_OUTPUT"
            fi
          fi
          if [ "$STATE" = "promoted" ]; then echo "Already promoted — idempotent no-op."; echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; fi
          {
            echo "skip=false"
            echo "tag=$(yq -p=json -e '.immutable_tag' "$F")"
            echo "digest=$(yq -p=json -e '.digest' "$F")"
            echo "image_ref=$(yq -p=json -e '.image_ref' "$F")"
            echo "base_image=$(yq -p=json -e '.base_image' "$F")"
            echo "color=$COLOR"
            echo "obj=$(yq -p=json -e '.standby_object' "$F")"
            echo "eligible_epoch=$(yq -p=json -e '.promote_eligible_after_epoch' "$F")"
          } >> "$GITHUB_OUTPUT"

      # ----- Reconcile shortcut: record the already-done flip, skip the rest. -----
      - name: Reconcile partial-promote (record only)
        if: steps.cand.outputs.reconcile == 'true'
        run: |
          set -euo pipefail
          NOW=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
          yq -i '.state = "promoted" | .promoted_timestamp_utc = "'"$NOW"'"' .github/state/candidate.json
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git config user.name  "github-actions[bot]"
          git add .github/state/candidate.json
          git commit -m "ci(reconcile): record prior flip -> ${{ steps.cand.outputs.color }} [skip ci]" || true
          for i in 1 2 3 4 5; do
            git fetch origin "${{ github.ref_name }}"
            git rebase "origin/${{ github.ref_name }}" && git push origin "HEAD:${{ github.ref_name }}" && exit 0
            sleep $((i*3))
          done
          echo "::error::reconcile push failed"; exit 1

      # ----- GATE A: soak >= 72h (epoch integer; no GNU date dependency). -----
      - name: Gate A — soak >= 72h
        if: steps.cand.outputs.skip == 'false' && steps.cand.outputs.reconcile != 'true'
        id: age
        run: |
          set -euo pipefail
          NOW=$(date -u +%s)
          ELIG="${{ steps.cand.outputs.eligible_epoch }}"
          if [ "$NOW" -lt "$ELIG" ]; then
            echo "Soak not complete: ~$(( (ELIG - NOW) / 3600 ))h remaining. Exiting clean."
            echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0
          fi
          echo "Soak satisfied."; echo "skip=false" >> "$GITHUB_OUTPUT"

      - name: Log in to Docker Hub
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        uses: docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772  # v3.4.0
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Assert fresh Trivy DB reachable
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          curl -sSf -o /dev/null --max-time 30 https://ghcr.io/v2/aquasecurity/trivy-db/tags/list \
            || { echo "::error::Trivy DB registry unreachable; refusing to promote on stale DB."; exit 1; }

      # ----- GATE B: re-scan the EXACT soaked digest with a FRESH DB. -----
      - name: Gate B — re-scan staged digest (fresh DB)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25  # v0.36.0
        env:
          TRIVY_SKIP_DB_UPDATE: "false"
        with:
          image-ref: ${{ steps.cand.outputs.image_ref }}
          scan-type: image
          scanners: vuln
          vuln-type: os,library
          severity: 'HIGH,CRITICAL'
          ignore-unfixed: true
          format: table
          exit-code: '1'

      # ----- GATE B2: cosign verify + base-digest unchanged (image-substitution defense). -----
      - name: Gate B2 — cosign verify signed digest
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          cosign verify "${{ steps.cand.outputs.image_ref }}" \
            --certificate-identity-regexp '^https://github.com/gregheffner/cicd/' \
            --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' >/dev/null \
            || { echo "::error::cosign verify FAILED for soaked digest — refusing to promote."; exit 1; }
          echo "cosign verify OK."

      - name: Gate B2b — base image digest unchanged
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          # Re-resolve what the standby manifest's FROM was recorded as; assert it still resolves.
          BASE="${{ steps.cand.outputs.base_image }}"
          case "$BASE" in *@sha256:*) ;; *) echo "::error::recorded base not digest-pinned: $BASE"; exit 1 ;; esac
          docker manifest inspect "$BASE" >/dev/null \
            || { echo "::error::recorded base digest $BASE no longer resolvable; refusing to promote."; exit 1; }
          echo "Base digest $BASE still valid."

      # ----- GATE C: standby Ready AND live == expected pre-flip peer. -----
      - name: Gate C — standby Ready + strict drift check
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          LIVE=$(kubectl get svc nginx-service -n prod -o jsonpath='{.spec.selector.version}')
          STANDBY="${{ steps.cand.outputs.color }}"
          EXPECT_LIVE="$( [ "$STANDBY" = "blue" ] && echo green || echo blue )"
          if [ "$LIVE" != "$EXPECT_LIVE" ]; then
            echo "::error::Live=$LIVE but candidate expects pre-flip peer=$EXPECT_LIVE. Drift/out-of-band change. Aborting."
            exit 1
          fi
          kubectl rollout status "${{ steps.cand.outputs.obj }}" -n prod --timeout=300s
          kubectl wait --for=condition=ready pod -l app=nginx-web,version="$STANDBY" -n prod --timeout=120s

      # ----- Verify running image == soaked digest (retry through transient zero-pod). -----
      - name: Verify standby running image == soaked digest
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          WANT="${{ steps.cand.outputs.digest }}"
          for i in 1 2 3 4 5 6; do
            RUNNING=$(kubectl get pods -n prod -l app=nginx-web,version="$STANDBY" \
              -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="nginx")].imageID}{"\n"}{end}' | sort -u)
            CNT=$(echo "$RUNNING" | grep -c . || true)
            if [ "$CNT" -gt 0 ]; then
              echo "Running nginx imageIDs on $STANDBY:"; echo "$RUNNING"
              # single-arch build -> running imageID digest == captured manifest digest
              if echo "$RUNNING" | grep -q "$WANT"; then echo "Confirmed soaked digest $WANT."; exit 0; fi
              echo "::error::Standby NOT running soaked digest. want=$WANT got=$RUNNING"; exit 1
            fi
            echo "no pods yet (attempt $i); retrying"; sleep 10
          done
          echo "::error::No standby pods after retries; refusing to flip."; exit 1

      # ----- Ensure HPA for incoming-live color (HARD block if not a Deployment/HPA). -----
      - name: Ensure HPA for incoming-live color (block non-autoscaling)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          KIND=$(kubectl get "${{ steps.cand.outputs.obj }}" -n prod -o jsonpath='{.kind}')
          if [ "$KIND" != "Deployment" ]; then
            echo "::error::Incoming color $STANDBY is a $KIND, not a Deployment. Auto-promote onto a non-autoscaling color is forbidden (prereq P2)."
            exit 1
          fi
          kubectl apply -f shared/nginx-hpa-${STANDBY}.yaml
          kubectl get hpa nginx-web-${STANDBY} -n prod

      # ----- HARD endpoint gate immediately before the flip. -----
      - name: Pre-flip — assert >=1 READY endpoint on target
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          READY=$(kubectl get endpointslices -n prod \
            -l kubernetes.io/service-name=nginx-service \
            -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{" "}{.targetRef.name}{"\n"}{end}' \
            | grep -c '^true ' || true)
          # endpointslices for the Service include only the LIVE selector's pods until flip;
          # so verify the target color pods are Ready directly instead.
          RP=$(kubectl get pods -n prod -l app=nginx-web,version="$STANDBY" \
            -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
            | grep -c '^True' || true)
          [ "$RP" -ge 1 ] || { echo "::error::0 Ready pods on target $STANDBY immediately pre-flip. Aborting."; exit 1; }
          echo "$RP Ready target pods confirmed pre-flip."

      # ----- FENCE: re-read ledger immediately before the irreversible patch. -----
      - name: Fence — ledger digest/state unchanged since start
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          git fetch origin "${{ github.ref_name }}"
          NOWDIGEST=$(git show "origin/${{ github.ref_name }}:.github/state/candidate.json" | yq -p=json -e '.digest')
          NOWSTATE=$(git show "origin/${{ github.ref_name }}:.github/state/candidate.json" | yq -p=json -e '.state')
          [ "$NOWDIGEST" = "${{ steps.cand.outputs.digest }}" ] || { echo "::error::Ledger digest changed since gate start (a build superseded this candidate). Aborting flip."; exit 1; }
          [ "$NOWSTATE" = "soaking" ] || { echo "::error::Ledger state changed to $NOWSTATE since start. Aborting flip."; exit 1; }
          echo "Fence OK: ledger still $NOWDIGEST / soaking."

      # ----- THE FLIP (Service-doc-scoped operations only). -----
      - name: Flip Service selector to standby (atomic)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          kubectl patch svc nginx-service -n prod --type merge \
            -p "{\"spec\":{\"selector\":{\"app\":\"nginx-web\",\"version\":\"${STANDBY}\"}}}"
          NEW=$(kubectl get svc nginx-service -n prod -o jsonpath='{.spec.selector.version}')
          [ "$NEW" = "$STANDBY" ] || { echo "::error::Selector did not flip (now=$NEW)"; exit 1; }
          echo "Live color is now $STANDBY."

      - name: Post-flip smoke test (poll all endpoints; rollback on failure)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          PREV="$( [ "$STANDBY" = "blue" ] && echo green || echo blue )"
          IP=$(kubectl get svc nginx-service -n prod -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
          ok=0
          for i in 1 2 3 4 5 6 7 8; do
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://${IP}/" || echo 000)
            echo "smoke $i: HTTP $code"
            [ "$code" = "200" ] && { ok=1; break; }
            sleep 5
          done
          if [ "$ok" != "1" ]; then
            echo "::error::Smoke failed after flip; rolling back to $PREV."
            kubectl patch svc nginx-service -n prod --type merge \
              -p "{\"spec\":{\"selector\":{\"app\":\"nginx-web\",\"version\":\"${PREV}\"}}}"
            exit 1
          fi
          echo "Smoke passed on $STANDBY."

      - name: Remove soak egress NetworkPolicy from now-live color
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          kubectl delete networkpolicy "soak-deny-egress-${{ steps.cand.outputs.color }}" -n prod --ignore-not-found

      # ----- GitOps persist: Service-doc-scoped yq (does NOT touch HPA; HPA now in its own file). -----
      - name: Update committed Service selector (Service doc only)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          yq -i '(select(.kind == "Service") | .spec.selector.version) = "'"$STANDBY"'"' shared/nginx-service.yaml
          grep -n 'version:' shared/nginx-service.yaml | head

      # ----- Pin the NOW-PREVIOUS color manifest to the SAME promoted digest too, -----
      # ----- so NO committed manifest ever contains :latest (Saturday-cron leak fix). -----
      - name: Pin previous-color manifest to promoted digest (sed, surgical)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          STANDBY="${{ steps.cand.outputs.color }}"
          DIGEST="${{ steps.cand.outputs.digest }}"
          PREV="$( [ "$STANDBY" = "blue" ] && echo green || echo blue )"
          PREVFILE="$( [ "$PREV" = "blue" ] && echo prod/nginx-blue.yaml || echo DR/nginx-green.yaml )"
          sed -i -E 's#(image: )technotuba/nginx[:@][^[:space:]"]*#\1technotuba/nginx@'"$DIGEST"'#' "$PREVFILE"
          grep -n 'technotuba/nginx' "$PREVFILE"

      - name: Update README Prod Silo badge
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          C="${{ steps.cand.outputs.color }}"
          sed -i 's|<img alt="Prod Silo" src="https://img.shields.io/badge/Prod%20Silo-[^"]*">|<img alt="Prod Silo" src="https://img.shields.io/badge/Prod%20Silo-'"$C"'-'"$C"'?style=for-the-badge">|' README.md || true

      - name: Mark candidate promoted
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          NOW=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
          yq -i '.state = "promoted" | .promoted_timestamp_utc = "'"$NOW"'"' .github/state/candidate.json
          cat .github/state/candidate.json

      - name: Purge Cloudflare cache
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ZONE_ID: ${{ secrets.CLOUDFLARE_ZONE_ID }}
        run: |
          set -euo pipefail
          curl -sSf -X POST "https://api.cloudflare.com/client/v4/zones/${CLOUDFLARE_ZONE_ID}/purge_cache" \
            -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json" \
            --data '{"purge_everything":true}'

      # State-commit retried independently so a flip is never left unrecorded.
      - name: Commit promotion (fetch+rebase+retry)
        if: steps.cand.outputs.skip == 'false' && steps.age.outputs.skip == 'false'
        run: |
          set -euo pipefail
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git config user.name  "github-actions[bot]"
          git add shared/nginx-service.yaml prod/nginx-blue.yaml DR/nginx-green.yaml README.md .github/state/candidate.json
          if git diff --cached --quiet; then echo "nothing to commit"; exit 0; fi
          git commit -m "ci(promote): live -> ${{ steps.cand.outputs.color }} (${{ steps.cand.outputs.tag }}) [skip ci]"
          for i in 1 2 3 4 5 6 7 8; do
            git fetch origin "${{ github.ref_name }}"
            git rebase "origin/${{ github.ref_name }}" && git push origin "HEAD:${{ github.ref_name }}" && { echo pushed; exit 0; }
            echo "retry $i"; sleep $((i*3))
          done
          echo "::error::Failed to push promotion (cluster IS flipped; reconcile path will record on next run)"; exit 1
```

---

## 5. `.github/state/candidate.json` (initial bootstrap)

Commit this once so the very first `soak-gate-promote` run exits clean (state `promoted` = idempotent no-op) and the first real build supersedes it. Reflects current live = green.

```json
{
  "schema": 2,
  "immutable_tag": "bootstrap",
  "digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "image_ref": "technotuba/nginx@sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "base_image": "bootstrap",
  "standby_color": "blue",
  "standby_object": "deployment/nginx-web-blue",
  "live_color_at_build": "green",
  "build_timestamp_utc": "2026-06-20T00:00:00Z",
  "build_epoch": 0,
  "build_scan_status": "pass",
  "promote_eligible_after_epoch": 0,
  "state": "promoted",
  "promoted_timestamp_utc": "2026-06-20T00:00:00Z",
  "run_url": "bootstrap"
}
```

---

## 6. `.trivyignore` — DELETED FROM THE DESIGN

There is **no `.trivyignore` file and none is created**. The audit found zero CVEs needing suppression: the live OpenSSL HIGH/MEDIUMs are **fixed by `apk upgrade`** (not by ignoring), and `ignore-unfixed: true` already prevents unfixable noise from blocking. The `trivyignores:` input is **removed from all three Trivy steps** (done above). If a future genuinely-unfixable CVE must be suppressed, add a `.trivyignore.yaml` then with per-CVE entries each carrying an expiry + justification, and a CI step that fails on an expired entry — do not ship a blanket ignore.

---

## 7. `generate_dockerfile.py` — final changes

Four edits. Note: `timeout=10` is **already present on line 21** (verified) — do not re-add it there.

### 7a. Digest-pin the base image (new helper + emit `FROM ...@sha256:...`)

Add this helper after `get_stable_nginx_version()` and use it to build `base_image`:

```python
def resolve_base_digest(tag_ref):
    """Resolve a floating tag like 'nginx:1.30-alpine-slim' to an immutable
    nginx@sha256:... ref so the soaked image's base layer is reproducible and
    a poisoned upstream retag cannot silently enter between builds."""
    repo = "library/nginx"
    tag = tag_ref.split(":", 1)[1]
    token = requests.get(
        "https://auth.docker.io/token",
        params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
        timeout=10,
    ).json()["token"]
    r = requests.get(
        f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.docker.distribution.manifest.list.v2+json,"
                      "application/vnd.oci.image.index.v1+json,"
                      "application/vnd.docker.distribution.manifest.v2+json",
        },
        timeout=10,
    )
    r.raise_for_status()
    digest = r.headers["Docker-Content-Digest"]
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"Could not resolve digest for {tag_ref}: {digest}")
    return digest
```

Then change the base-image construction (current line 71):

```python
base_image = f"nginx:{NGINX_VERSION}-alpine-slim"
```

to:

```python
_base_tag = f"nginx:{NGINX_VERSION}-alpine-slim"
_base_digest = resolve_base_digest(_base_tag)
base_image = f"nginx@{_base_digest}"   # immutable; human tag kept in a comment below
```

And change the `FROM` line in the heredoc (current line 75) to record the human tag:

```python
FROM {base_image}
# base tag at build time: {_base_tag}
```

### 7b. Stable-branch filter in `get_stable_nginx_version()` (replace lines 41–44)

```python
    # Sort and get the latest
    latest = max(version_tags, key=version.parse)
    print(f"[INFO] Latest stable nginx version found: {latest}")
    return latest
```

→

```python
    # nginx STABLE branches have an EVEN minor (1.28, 1.30, ...); ODD is mainline
    # (1.29, 1.31). max() over all tags picks mainline despite this function's name,
    # so filter to the stable (even-minor) branch first.
    stable_tags = [v for v in version_tags if version.parse(v).release[1] % 2 == 0]
    if not stable_tags:
        print("[WARN] No even-minor stable tags; falling back to newest available.")
        stable_tags = version_tags
    latest = max(stable_tags, key=version.parse)
    print(f"[INFO] Latest STABLE nginx version found: {latest}")
    return latest
```

### 7c. `apk upgrade` + COPY-before-chmod, drop `|| true` (replace current lines 88–95 in the heredoc)

```
RUN set -x && apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/America/New_York /etc/localtime && \
    echo "America/New_York" > /etc/timezone && \
    chmod +x /docker-entrypoint.sh && \
    chmod +x /docker-entrypoint.d/*.sh || true

COPY DockerImage/docker-entrypoint.sh /
COPY DockerImage/docker-entrypoint.d/ /docker-entrypoint.d/
```

→

```
# apk runs pre/post-install + trigger scripts as ROOT at BUILD time -> keep this on the
# ephemeral runner only. apk upgrade picks up openssl 3.5.6-r0 -> 3.5.7-r0 (ABI-stable .so swap).
RUN set -eux && \
    apk upgrade --no-cache && \
    apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/America/New_York /etc/localtime && \
    echo "America/New_York" > /etc/timezone

COPY DockerImage/docker-entrypoint.sh /
COPY DockerImage/docker-entrypoint.d/ /docker-entrypoint.d/

# chmod AFTER the COPY (the previous chmod-before-COPY was a no-op masked by `|| true`).
RUN set -eux && \
    chmod +x /docker-entrypoint.sh && \
    chmod +x /docker-entrypoint.d/*.sh
```

### 7d. Timeouts on the two remaining njs/Debian calls

Both `resp = requests.get(url)` lines (verified at **lines 49 and 56**) lack a timeout. Apply with the Edit tool using `replace_all: true` on the exact string `    resp = requests.get(url)` → `    resp = requests.get(url, timeout=10)`. (Line 21 already has the timeout and won't match.)

---

## 8. `shared/nginx-service.yaml` — split HPA out (mandatory, fixes yq-corruption blocker) + `Cluster`

Replace the whole file with **just the Service** (no `---` HPA document), and switch to `externalTrafficPolicy: Cluster` (after P1 fail2ban verification):

```yaml
apiVersion: v1
kind: Service
metadata:
  labels:
    app: nginx-web
    k8slens-edit-resource-version: v1
  name: nginx-service
  namespace: prod
spec:
  externalTrafficPolicy: Cluster   # was Local; Cloudflare provides real client IP via CF-Connecting-IP (verify fail2ban first, P1)
  ipFamilyPolicy: SingleStack
  ports:
    - name: http
      port: 80
    - name: nginx-status
      port: 81
  selector:
    app: nginx-web
    version: green
  type: LoadBalancer
```

New `shared/nginx-hpa-blue.yaml`:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: nginx-web-blue
  namespace: prod
spec:
  maxReplicas: 10
  minReplicas: 3
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: nginx-web-blue
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization        # was AverageValue 3000m vs 100m request = never fired
          averageUtilization: 70
```

New `shared/nginx-hpa-green.yaml` (identical, `name: nginx-web-green`, `scaleTargetRef.name: nginx-web-green`, `kind: Deployment` — requires P2):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: nginx-web-green
  namespace: prod
spec:
  maxReplicas: 10
  minReplicas: 3
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: nginx-web-green
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

---

## 9. Color manifest changes (`prod/nginx-blue.yaml`, `DR/nginx-green.yaml`)

These keep their leading-2-space indentation; apply with the Edit tool (surgical, NOT yq). **Image is left as-is for now and pinned by the pipeline** (build pins standby, promote pins the previous color) — but you bootstrap the live color's pin once in §11 step 4.

For **BOTH** files, on the `nginx` container, after the `ports:` block and alongside `resources:`, add a readiness probe, raise the CPU request, and add a drain. Also set the soak SA + token de-mount and a PDB. Concretely:

1. **`DR/nginx-green.yaml`: change `kind: DaemonSet` → `kind: Deployment`** and add under `spec:` (sibling of `selector:`): `replicas: 3` (P2).
2. In **both** nginx containers add:
   ```yaml
            readinessProbe:
              httpGet: { path: /, port: 80 }
              initialDelaySeconds: 3
              periodSeconds: 5
              failureThreshold: 3
            lifecycle:
              preStop:
                exec: { command: ["/bin/sh","-c","sleep 5; nginx -s quit"] }
   ```
   and change `requests.cpu: 100m` → `requests.cpu: 250m` (P5).
3. In **both** pod specs set `terminationGracePeriodSeconds: 30` (P4) and **keep `serviceAccountName: prod`** for the LIVE role — but the pipeline-deployed standby gets isolation via a patch in deploy-standby is NOT used; instead the simplest correct approach: set on both pod templates `automountServiceAccountToken: false` (P6) — nginx serving static content needs no API access in either role; the fail2ban sidecar talks to Cloudflare over the network, not the kube-API, so de-mounting the token is safe. This neutralizes the "standby reads SA token" blocker for the soak AND hardens live.
4. **Change `imagePullPolicy: Always` → `imagePullPolicy: IfNotPresent`** in both (digest refs are immutable; `Always` is pointless and re-pull-on-bounce is the `:latest` leak vector).
5. The `images-volume` hostPath stays `type: Directory` — **operator must confirm `/home/huey/www/images` exists on every node** both Deployments can schedule to (was implicitly satisfied only because green was a DaemonSet on nodes that have it). See residual risks.

New `shared/nginx-pdb.yaml` (one per color):

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: nginx-web-blue
  namespace: prod
spec:
  minAvailable: 2
  selector:
    matchLabels: { app: nginx-web, version: blue }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: nginx-web-green
  namespace: prod
spec:
  minAvailable: 2
  selector:
    matchLabels: { app: nginx-web, version: green }
```

---

## 10. DELETION LIST

```bash
git rm .github/workflows/monthly-docker-image-retag.yaml \
       .github/workflows/update-blue-deployment-to-latest.yaml \
       .github/workflows/switch-traffic-to-blue.yaml \
       .github/workflows/switch-traffic-to-green.yaml
```

Keep `generate_dockerfile.py` (reused by the new build). Edit `delete-kubernetes-pods.yaml`: **remove `prod` from both the choice list and the `for` loop**, and add `concurrency: { group: nginx-pipeline, cancel-in-progress: false }` (P7). README: remove the three deleted-workflow badge rows; also remove the pre-existing dangling `Push Green To Latest` row (no such workflow exists); add two rows for the new workflows. Documentation-only.

---

## 11. STEP-BY-STEP IMPLEMENTATION ORDER

**Step 0 — Verify the environment (do not skip).**
- `gh api /repos/<owner>/<repo>/git/refs/tags` for each pinned action (checkout, buildx, setup-python, cosign-installer, login, build-push, trivy-action) and confirm each SHA matches its claimed tag. Fix any mismatch.
- On the self-hosted runner confirm: `docker`, `cosign`, `kubectl`, GNU `wget` present; kubeconfig works (`kubectl get svc nginx-service -n prod`).
- Confirm CNI is flannel (it is — `kube-flannel` ns) → treat NetworkPolicy as best-effort.
- **P1 check:** inspect the fail2ban cloudflare-ban filter/jail; confirm it bans on `CF-Connecting-IP`/`X-Forwarded-For`, not the TCP source. If not, fix that BEFORE switching to `Cluster`.
- Confirm `/home/huey/www/images` exists on every schedulable node.

**Step 1 — Land the prerequisites (P1–P7) in one PR, cron NOT yet added.** Convert green to a Deployment, add readiness probes + preStop + `terminationGracePeriodSeconds` + `automountServiceAccountToken:false` + `requests.cpu:250m` + `imagePullPolicy:IfNotPresent` to both colors; split the HPA into per-color files; add PDBs; set Service to `Cluster`; scope `prod` out of delete-pods and add its concurrency group. **Apply these to the cluster manually** (`kubectl apply -f`) and confirm both colors run Ready with the probe, HPAs attach, green now scales.

**Step 2 — Bootstrap the live color's digest pin.** Resolve the current live (`green`) image to a digest and pin BOTH color manifests to it so no manifest contains `:latest`:
```bash
DIGEST=$(docker buildx imagetools inspect technotuba/nginx:latest --format '{{json .Manifest.Digest}}' | tr -d '"')
sed -i -E 's#(image: )technotuba/nginx[:@][^[:space:]"]*#\1technotuba/nginx@'"$DIGEST"'#' DR/nginx-green.yaml prod/nginx-blue.yaml
```
Commit, `kubectl apply` both, confirm pods still Ready.

**Step 3 — Commit the bootstrap ledger** (`.github/state/candidate.json`, state `promoted`, §5) and the two new workflow files. Commit the deletions (§10).

**Step 4 — Dry-run promote (no real flip).** Manually `workflow_dispatch` `soak-gate-promote`. With the bootstrap ledger it must **exit clean at the "already promoted" check**. Confirms wiring/permissions/yq/cosign without touching the cluster.

**Step 5 — First real build via `workflow_dispatch` on `build-stage-scan`.** Watch: gate runs before push; cosign signs; standby (blue) deploys digest-pinned; ledger written `soaking` with a +72h epoch; standby Ready. Live stays green — **zero customer impact.**

**Step 6 — Force-promote dry-run before trusting the clock.** To validate the full flip path without waiting 72h, temporarily `workflow_dispatch` `soak-gate-promote` and confirm it exits at Gate A (soak not complete) — proving the clock gate works. Then, in a controlled window, edit the ledger's `promote_eligible_after_epoch` to the past on a branch and dispatch once to watch a real green→blue flip + smoke + auto-rollback-on-failure behavior, with you watching. Flip back manually (§ rollback) and reset the ledger if needed.

**Step 7 — Enable the crons.** Only after a clean manual full-cycle: leave the `schedule:` triggers in place. First automatic promote occurs the Thursday after the first Monday build.

**Step 8 — Protect `main`** so only the CI bot can write `candidate.json` (prevents back-dating the soak clock). Document that the self-hosted runner + DOCKERHUB/CLOUDFLARE secrets are the trust root.

---

## 12. ROLLBACK

The previous color is never scaled down (build touches only standby; promote only patches the selector and re-pins the previous manifest to the **same** promoted digest). Instant manual rollback:

```bash
kubectl get svc nginx-service -n prod -o jsonpath='{.spec.selector.version}'; echo
kubectl patch svc nginx-service -n prod --type merge \
  -p '{"spec":{"selector":{"app":"nginx-web","version":"<prev>"}}}'
yq -i '(select(.kind == "Service") | .spec.selector.version) = "<prev>"' shared/nginx-service.yaml
git add shared/nginx-service.yaml && git commit -m "ops(rollback): revert live -> <prev> [skip ci]" && git push
```
Automatic rollback is built into the post-flip smoke step. Rollback recovers **image** regressions only; shared `www-configmap`/`nginx-config` content is not fixed by a color flip.

---

## 13. RESIDUAL RISKS / explicitly-deferred hardening

1. **Soak is a CVE-disclosure-window mechanism, not a containment boundary.** On flannel the egress NetworkPolicy is **not enforced**. The real isolation is `automountServiceAccountToken:false` (no API token to steal) + the ephemeral, credential-free build runner. A startup-triggered implant in the standby still runs on prod nodes with access to mounted configmaps and the `/home/huey/www/images` + `/var/log/nginx` hostPaths, and (since the fail2ban sidecar is `privileged:true`) node-level pivot is possible. **Stronger fix (deferred):** soak in a separate namespace / tainted node pool, and drop the privileged fail2ban sidecar from the soaking pod. Stated loudly: the 3-day soak buys ecosystem-detection time and a fresh-DB re-scan; it does not contain a determined startup-active backdoor.

2. **Novel-backdoor detection is limited.** Trivy matches **known** CVEs/yanked packages only. cosign verify + base-digest pin defend against image **substitution** between build and promote; they do not detect an xz-style upstream implant that was never catalogued. SBOM diff at promote is a recommended future addition.

3. **`purge_everything` thundering herd.** Full cache purge after the flip spikes origin load on the freshly-flipped color. With the corrected HPA (Utilization 70%) and symmetric Deployments this is tolerable for static content; prefer targeted purge later.

4. **hostPath `type: Directory` on `/home/huey/www/images`** must exist on every node both Deployments can schedule to; a missing dir now correctly keeps a pod out of endpoints (readiness probe + `Cluster` policy), but it reduces capacity. Migrate to a shared volume to remove the per-node dependency (deferred).

5. **In-flight keep-alive connections** to the old color persist until closed; the preStop drain + `terminationGracePeriodSeconds` handle graceful teardown only when the old color is eventually scaled/bounced, not at the flip itself (the flip intentionally does not touch old pods — that is what makes instant rollback possible).

**Blockers status:** all are resolved **conditional on the P1–P8 prerequisites actually landing and being applied to the cluster (Steps 1–2) before the crons are enabled (Step 7).** The one blocker that cannot be fully eliminated in this architecture is soak-as-containment (residual risk 1) — the **safest interim** is the token de-mount + ephemeral runner above, with the honest caveat that true containment requires soaking off the live prod nodes.

> ⚠️ REALIZED STATE (2026-06-21): keyless cosign signing is IMPLEMENTED in
> `.github/workflows/build-stage-scan.yaml` (sign+verify the pushed digest) and
> re-verified in `.github/workflows/soak-gate-promote.yaml`. The trusted identity is
> `^https://github\.com/gregheffner/cicd/\.github/workflows/build-stage-scan\.yaml@refs/heads/main$`
> (issuer `https://token.actions.githubusercontent.com`). The workflow names in the
> plan below (`monthly-docker-image-retag.yaml`, `update-blue-deployment-to-latest.yaml`,
> `switch-traffic-*`) are from the EARLIER design and have since been DELETED/replaced —
> read them as historical. Kyverno cluster admission is still a standalone NOT-YET-DONE follow-up.
>
> Status: APPROVED by Greg (keyless). Adversarially verified 2026-06-20.
> Version note: Kyverno chart 3.8.1 (app v1.18.1), cosign v2.6.3 — confirm on cluster before deploy.

# Cosign Supply-Chain Hardening — FINAL (corrected) — gregheffner/cicd / technotuba/nginx

This is the implementation-ready plan. It supersedes the prior draft. Every blocker from the critique is folded in and the artifacts below are paste-ready and consistent with the **actual** workflow/manifest contents verified on `main`.

> The single most important correction: in the prior draft the CI verify gate sat on `switch-traffic-to-blue.yaml`, which only flips the Service selector and **never sets the deployed image**. The gate has been moved onto the two workflows that actually write+commit the image (`monthly-docker-image-retag.yaml`, `update-blue-deployment-to-latest.yaml`), and the existing `sed` steps that rewrite the image back to a **mutable tag** have been rewritten to write the **verified `@sha256` digest**. Without both fixes the digest pin self-reverts on the next scheduled run and signing is meaningless.

---

## 1. EXECUTIVE SUMMARY

| Layer | What it does | Where it runs | The honest limit |
|---|---|---|---|
| **Sign (keyless)** | `cosign sign` the **pushed digest** + SBOM attest, via GitHub OIDC → Fulcio/Rekor. No long-lived key. | GitHub `ubuntu-latest` (in `monthly-docker-image-retag.yaml`) | Signing alone constrains nothing. It only creates an attestation the later layers can check. |
| **CI verify gate** | `cosign verify` the resolved digest against issuer + anchored identity regexp, **before** the manifest-commit step. Manifest is then pinned to that exact `@sha256`. | GitHub `ubuntu-latest`, **inside the image-writing workflows** | Protects the pipeline path only. Does **not** stop ArgoCD selfHeal from deploying any other committed digest. Capped by who can commit to `main` / push tags. |
| **Cluster admission (Kyverno)** | Rejects any `technotuba/nginx*` pod whose image is not signed by the pinned identity. The **only** layer that constrains what actually runs. | Kyverno admission pods (works under flannel) | Scoped to `technotuba/nginx*` only — fail2ban and system images pass unverified by design. Fail-closed → a Sigstore outage can block prod pod (re)admission. Strength capped by `main`/tag write-access. |

**What we sign:** the immutable `technotuba/nginx@sha256:…` digest (never a tag), plus a CycloneDX SBOM attestation.
**Where we verify:** (a) in CI before the manifest is committed, on the same digest we then pin; (b) at the cluster on every matching pod admission.
**How the cluster enforces:** Kyverno `ClusterPolicy`, ns `prod`, `imageReferences: technotuba/nginx*` only, keyless attestor, `Audit` → `Enforce`.
**The decision — KEYLESS:** image+repo are PUBLIC, GitHub-hosted runners with egress, no air-gap. The only downside of keyless (the Rekor public-log entry exposing repo/workflow/digest/timestamp) leaks nothing not already public, and in exchange you eliminate every long-lived signing secret. A raw key adds rotation/leak burden for zero benefit; a KMS key is only worth it if you later need to survive a Sigstore outage or go air-gapped. **Keyless is correct; do not add a key.**

**LOUD residual blocker that is NOT solved by any artifact here:** Kyverno's strength is capped by repo write-access. If `main` is unprotected or git **tags** can be pushed by anyone (tags are usually *not* covered by branch protection), an attacker can run the signing workflow on a tag ref, mint a fully valid signature, and pass both the CI gate and Kyverno. The identity regexp below is therefore pinned to **`refs/heads/main` only** (tag refs dropped), and §4 lists branch+tag protection as a **required** manual step. Until that protection exists, treat cluster enforcement as defense-in-depth, not a guarantee.

---

## The single trust anchor (these THREE must stay byte-identical)

Trust is defined in three places: the implicit `cosign sign` identity, the CI `cosign verify` gate, and the Kyverno attestor. They must agree exactly. Renaming/moving the signing workflow is a "rotation" that updates all three in one commit.

- **OIDC issuer (literal, never a regexp):** `https://token.actions.githubusercontent.com`
- **Identity (anchored, dot-escaped, pinned to `main` ONLY — tag refs deliberately removed):**
  ```
  ^https://github\.com/gregheffner/cicd/\.github/workflows/build-stage-scan\.yaml@refs/heads/main$
  ```
  Anchored `^…$`, dots escaped (an unanchored fragment would also match `cicd-evil`). **Never** a wildcard identity (`'.+'`) — with public Fulcio, *anyone* can mint a valid Sigstore signature, so a wildcard verifies "signed by someone," not "signed by MY workflow." Tag refs are excluded because the monthly workflow runs on `schedule`/`workflow_dispatch` against `main` and never needs a tag ref to sign; allowing `tags/*` would let an unprotected tag push mint a valid signature.

---

## 2. FINAL ARTIFACTS

### 2A. SIGNING — edits to `.github/workflows/monthly-docker-image-retag.yaml`

**Edit A — job permissions (replace lines 10-11).** `id-token: write` mints the OIDC token for Fulcio. Keep `contents: write` (the job commits manifests). No `packages: write` (that's GHCR; your registry is Docker Hub — cosign reuses the existing `docker/login-action` session to push `.sig`/`.att`).

```yaml
    permissions:
      contents: write   # existing: job commits manifest/README updates
      id-token: write   # NEW: mints GitHub OIDC token for Fulcio (keyless signing)
```

**Edit B — replace the bare push (line 77-78) with a digest-capturing push.** Sign the digest, never the tag.

```yaml
      - name: Push image after passing scan gate (capture digest)
        id: push
        run: |
          set -euo pipefail
          docker push technotuba/nginx:latest
          DIGEST="$(docker buildx imagetools inspect technotuba/nginx:latest \
            --format '{{json .Manifest.Digest}}' | tr -d '\"')"
          [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "bad digest: $DIGEST"; exit 1; }
          echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"
          echo "Pushed digest: ${DIGEST}"
```

**Edit C — install cosign + sign the digest (insert immediately after the push step).**
**Signature-format decision (resolves the v3/OCI-1.1-referrers interop risk):** pin the signer to **cosign v2.x**, which emits the legacy `sha256-<digest>.sig` tag that every currently-released Kyverno chart's embedded cosign library verifies. cosign v3.0.x defaults to the new OCI 1.1 referrers/bundle format, which the pinned Kyverno chart may not verify — a silent-failure trap. Use v2 on **both** ends (signer + verify gate) and confirm the Kyverno chart's cosign before ever considering v3.

```yaml
      - name: Install cosign
        uses: sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159  # v3.9.2
        with:
          cosign-release: 'v2.5.0'    # legacy .sig output; matches Kyverno chart's cosign lib

      - name: Sign image by digest (keyless, Rekor on)
        env:
          DIGEST: ${{ steps.push.outputs.digest }}
        run: cosign sign --yes "technotuba/nginx@${DIGEST}"
```

> NOTE on the installer SHA: pin `sigstore/cosign-installer` to a SHA you confirm corresponds to a release that supports `cosign-release: v2.5.0`. Verify before merge with: `gh api repos/sigstore/cosign-installer/git/refs/tags/v3.9.2 -q .object.sha`. (The prior draft's installer-v4/cosign-v3 pairing is dropped specifically to keep legacy `.sig` output.)

**SBOM attestation (recommended; you already run Trivy). Add after signing:**

```yaml
      - name: Generate SBOM (CycloneDX)
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25  # v0.36.0
        with:
          image-ref: technotuba/nginx@${{ steps.push.outputs.digest }}
          scan-type: image
          format: cyclonedx
          output: sbom.cdx.json

      - name: Attest SBOM (keyless, bound to digest)
        env:
          DIGEST: ${{ steps.push.outputs.digest }}
        run: cosign attest --yes --type cyclonedx --predicate sbom.cdx.json "technotuba/nginx@${DIGEST}"
```

**SLSA provenance — DEFER** (`actions/attest-build-provenance`, also keyless, needs `attestations: write`) until the signature path is proven end-to-end.

---

### 2B. CRITICAL — rewrite the manifest-mutation steps so the digest pin does NOT self-revert

The existing `sed 's#technotuba/nginx:[^"]*#…:TAG#g'` steps rewrite the image back to a mutable tag on every run, and that regex (colon after `nginx`) won't even match an `@sha256` form. These are **mandatory** edits, in the **same change** as the digest pin.

**In `monthly-docker-image-retag.yaml`** — replace lines 99-103 (the two `sed` steps). Note: this workflow currently puts `LATEST` on green and `PREVIOUS` on blue using tag names pulled from Docker Hub. We replace that with digest pinning: green → the freshly signed digest; blue → the previously-pinned digest (read out of the current blue manifest before overwriting). Add a verify gate as a separate job that this commit step `needs`.

```yaml
      - name: Capture previous blue digest (for green->blue demotion model)
        id: prevdig
        run: |
          set -euo pipefail
          PREV="$(grep -oE 'technotuba/nginx@sha256:[0-9a-f]{64}' prod/nginx-blue.yaml | head -1 | cut -d@ -f2 || true)"
          echo "previous=${PREV:-$DIGEST}" >> "$GITHUB_OUTPUT"
        env:
          DIGEST: ${{ steps.push.outputs.digest }}

      - name: Pin GREEN (DR DaemonSet) to the newly signed digest
        run: |
          set -euo pipefail
          yq -i '(.spec.template.spec.containers[] | select(.name=="nginx") | .image)
                 = "technotuba/nginx@${{ steps.push.outputs.digest }}"' DR/nginx-green.yaml
          yq -i '(.spec.template.spec.containers[] | select(.name=="nginx") | .imagePullPolicy)
                 = "IfNotPresent"' DR/nginx-green.yaml

      - name: Pin BLUE (prod Deployment) to the previous signed digest
        run: |
          set -euo pipefail
          yq -i '(.spec.template.spec.containers[] | select(.name=="nginx") | .image)
                 = "technotuba/nginx@${{ steps.prevdig.outputs.previous }}"' prod/nginx-blue.yaml
          yq -i '(.spec.template.spec.containers[] | select(.name=="nginx") | .imagePullPolicy)
                 = "IfNotPresent"' prod/nginx-blue.yaml
```

> `yq` targets only the `name: nginx` container by path, so **fail2ban is never touched**. `yq` is not on k8-primary, but these steps run on `ubuntu-latest` where `yq` is preinstalled. Using `yq`-by-path (not `sed`) also future-proofs against the regex mismatch.
>
> IMPORTANT: the manifests are indented with **2 leading spaces on every line** (the `apiVersion`/`kind` start at column 3 — verified). `yq` rewrites the file and will normalize that indentation. That is cosmetic and ArgoCD-safe, but if you want to preserve the exact leading-space style, post-process is unnecessary — ArgoCD compares parsed objects, not bytes.

**In `update-blue-deployment-to-latest.yaml`** — this workflow has **no image push and no digest of its own**; it currently `sed`s blue back to `:latest`. Since the live image is whatever is already signed, the correct behavior is to pin blue to the **current `green` digest** (the standard "promote DR's running image to blue"), not to a tag. Replace line 28:

```yaml
      - name: Pin blue to the currently-signed green digest (no tags)
        run: |
          set -euo pipefail
          GREEN_DIG="$(grep -oE 'technotuba/nginx@sha256:[0-9a-f]{64}' DR/nginx-green.yaml | head -1 | cut -d@ -f2)"
          [[ "$GREEN_DIG" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "no green digest pinned"; exit 1; }
          yq -i "(.spec.template.spec.containers[] | select(.name==\"nginx\") | .image)
                 = \"technotuba/nginx@${GREEN_DIG}\"" prod/nginx-blue.yaml
          yq -i '(.spec.template.spec.containers[] | select(.name=="nginx") | .imagePullPolicy)
                 = "IfNotPresent"' prod/nginx-blue.yaml
```

This workflow then needs a `verify-signature` job (below) that the commit step `needs`, because it changes blue's running image.

---

### 2C. CI VERIFY GATE — first job in EACH image-writing workflow

Add this job to **`monthly-docker-image-retag.yaml`** and **`update-blue-deployment-to-latest.yaml`**, and make the manifest-commit step `needs: verify-signature`. Verify needs **registry read only** — no `id-token`, no cluster access, nothing on k8-primary. Resolve the digest deterministically (drop the fragile `cosign triangulate` branch entirely) and assert its shape before verifying.

```yaml
  verify-signature:
    runs-on: ubuntu-latest          # registry read only; NOTHING on k8-primary
    permissions:
      contents: read                # NO id-token needed to verify
    steps:
      - name: Install cosign
        uses: sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159  # v3.9.2
        with:
          cosign-release: 'v2.5.0'

      - name: Resolve digest and verify signature (BLOCKS on failure)
        run: |
          set -euo pipefail
          DIGEST="$(docker buildx imagetools inspect technotuba/nginx:latest \
                    --format '{{json .Manifest.Digest}}' | tr -d '\"')"
          [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "bad digest: $DIGEST"; exit 1; }
          echo "Verifying technotuba/nginx@${DIGEST}"
          cosign verify "technotuba/nginx@${DIGEST}" \
            --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
            --certificate-identity-regexp '^https://github\.com/gregheffner/cicd/\.github/workflows/build-stage-scan\.yaml@refs/heads/main$'
```

Then gate the commit job:

```yaml
  retag-latest:
    needs: [verify-signature]   # in monthly-retag: split sign(+push) and verify so verify runs on the pushed digest
    # ...
```

> Ordering note for the monthly workflow: `verify-signature` must run **after** the sign step (you can only verify a signed digest) but **before** the manifest-commit/push. Simplest structure: keep sign+push+SBOM in `retag-latest`, then have a `pin-and-commit` job that `needs: [retag-latest]` and contains the verify step inline immediately before the `yq` pin + `git commit/push`. For `update-blue`, `verify-signature` is a standalone gate job and the commit job `needs` it.

Do **not** add `--insecure-ignore-tlog` / `--insecure-ignore-sct` — those checks are the point.

**Belt-and-suspenders (optional):** keep a cheap verify-the-currently-pinned-digest sanity check in `switch-traffic-to-blue.yaml` / `switch-traffic-to-green.yaml`. It is *not* load-bearing (those workflows don't change the image) but catches a drifted manifest before a selector flip.

---

### 2D. CLUSTER ENFORCEMENT — Kyverno (the only layer that constrains what runs)

**Why CI is not enough:** ArgoCD does not verify signatures and selfHeal re-asserts the committed digest within ~3 min. The only place to constrain the cluster is an admission webhook. Webhooks work under flannel (flannel only fails to enforce NetworkPolicy; admission control is unaffected).

**Choice: Kyverno** over sigstore policy-controller: one reusable engine (you already gate with Trivy and may want broader pod policy), it excludes `kube-system`/its own namespace from webhooks by default, and policy-controller's `no-match-policy: deny` default would brick the unsigned fail2ban sidecar the moment you label ns `prod`.

**THE critical scoping rule (handles fail2ban + all third-party/system images):** prod pods run **two** containers — your signed `technotuba/nginx` and the third-party privileged **unsigned** `crazymax/fail2ban:latest`. A policy requiring signatures on *all* images rejects fail2ban → the **whole pod fails admission → site down**. Scope `imageReferences` to `technotuba/nginx*` ONLY; fail2ban, kube-system, flannel, argocd, datadog, everything else falls through unverified.

#### `security/verify-nginx-signature.yaml`

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-technotuba-nginx
spec:
  validationFailureAction: Audit      # PHASE C report-only. Flip to Enforce in PHASE E.
  webhookConfiguration:
    failurePolicy: Fail               # fail-closed (see break-glass in §4/§6)
  webhookTimeoutSeconds: 30
  background: false
  rules:
    - name: verify-nginx-keyless
      match:
        any:
          - resources:
              kinds: ["Pod"]
              namespaces: ["prod"]    # blue Deployment AND green DaemonSet both live in ns prod
      verifyImages:
        - imageReferences:
            - "docker.io/technotuba/nginx*"
            - "technotuba/nginx*"     # SCOPE: ONLY your image. fail2ban/system images untouched.
          required: true
          mutateDigest: true          # rewrite any residual tag -> verified @sha256 at admission
          verifyDigest: true
          attestors:
            - entries:
                - keyless:
                    issuer: "https://token.actions.githubusercontent.com"
                    subjectRegExp: "^https://github\\.com/gregheffner/cicd/\\.github/workflows/monthly-docker-image-retag\\.yaml@refs/heads/main$"
                    rekor:
                      url: https://rekor.sigstore.dev
                    ctlog: {}
```

Kyverno admission pods need egress to `fulcio.sigstore.dev` and `rekor.sigstore.dev`. **Verify that egress works before Enforce** (see Phase D).

#### `security/kyverno-app.yaml` — install Kyverno via ArgoCD, with sync waves

Two corrections from the prior draft: (1) `targetRevision: 3.2.6` does **not exist** — pinned to a real published version; (2) sync waves so the controller is Ready before the `ClusterPolicy` applies (prevents a selfHeal flap loop).

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: kyverno
  namespace: automation
  annotations:
    argocd.argoproj.io/sync-wave: "0"   # install controller+CRDs first
spec:
  project: default
  source:
    repoURL: https://kyverno.github.io/kyverno
    chart: kyverno
    targetRevision: 3.4.6               # REAL published version. Confirm: helm search repo kyverno/kyverno --versions
    helm:
      values: |
        admissionController:
          replicas: 3                   # HA across the 4 nodes
  destination:
    server: https://kubernetes.default.svc
    namespace: kyverno
  syncPolicy:
    automated:
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

> Put `security/verify-nginx-signature.yaml` in a path synced by an Application annotated `argocd.argoproj.io/sync-wave: "1"` (a later wave than Kyverno) so the policy applies only after the controller is Ready. Before merging, run `helm search repo kyverno/kyverno --versions` and confirm both that `3.4.6` exists and that its bundled cosign verifies the legacy `.sig` format your v2.5.0 signer emits.

---

## 3. THIRD-PARTY / UNSIGNED IMAGE HANDLING (so enforcement cannot brick prod)

- **fail2ban (`crazymax/fail2ban:latest`, privileged), both pods:** NOT covered by the policy. `imageReferences` is `technotuba/nginx*` only; fail2ban falls through unverified and admits normally. The `yq`-by-path manifest edits also only touch the `name: nginx` container, never fail2ban.
- **Datadog / system / flannel / argocd / kube-system images:** all unverified by scope; Kyverno also self-excludes `kube-system` and its own namespace by default.
- **Verification of the scoping is a hard gate before Enforce:** in Phase C (Audit) confirm fail2ban does **not** appear as a fail in PolicyReports. If it does, the scope is wrong — fix before flipping Enforce.

---

## 4. WHAT GREG MUST DECIDE / DO

1. **Keyless vs key — DECIDED: keyless.** No secret, no rotation. Revisit a KMS key only for a future Sigstore-outage-survival or air-gap need.
2. **One-time keyless setup: NONE.** No `generate-key-pair`, no new GH secrets, nothing on k8-primary. Existing `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` let cosign push `.sig`/`.att`.
3. **REQUIRED — protect `main` AND tags (this is the cap on Kyverno's strength).** Add a branch-protection rule / ruleset requiring PR review on `main`, and a **tag ruleset restricting who can create/push tags** (esp. `v*`). Ensure the `github-actions[bot]` / `GITHUB_TOKEN` has least privilege. Without this, scenario "attacker commits a foreign-but-signed digest" or "attacker pushes a tag to mint a valid signature" degrades cluster enforcement. (We already dropped tag refs from the identity regexp; protecting tags is the second half.)
4. **Confirm chart version + cosign interop before merge:** `helm search repo kyverno/kyverno --versions` → confirm `3.4.6` (or pick current stable), and confirm its cosign verifies legacy `.sig`. Confirm the `cosign-installer` SHA maps to a release supporting `cosign-release: v2.5.0`.
5. **Cut the live deployment over to a SIGNED digest BEFORE Enforce.** The currently-running `:latest` resolves to an **unsigned** digest (pushed before signing existed) and both manifests use `imagePullPolicy: Always`, so on the next restart/reschedule/selfHeal under Enforce those pods would be **rejected → site down**. The phased order below makes "blue+green pinned to a signed digest, pods recreated on it, Audit shows pass" an explicit gate before Enforce.
6. **Accept the fail-closed availability tradeoff + break-glass.** `failurePolicy: Fail` means a Sigstore outage (or broken cluster egress) blocks (re)admission of `technotuba/nginx*` pods. Break-glass: keep Kyverno verification-result caching on; pre-stage a one-line rollback (flip `validationFailureAction: Enforce → Audit`, committed via the same Application so selfHeal applies it within ~3 min). Optionally run the first Enforce window with `failurePolicy: Ignore` and tighten to `Fail` once egress reliability is proven.
7. **Decide SLSA provenance timing:** SBOM in Phase A (included); SLSA `attest-build-provenance` deferred.

---

## 5. PHASED ROLLOUT + TEST PLAN

**Order:** A (sign+attest) → B (CI verify gate + `yq` digest-pin steps) → **C+ (cut live blue+green over to the signed digest, recreate pods)** → C (Kyverno Audit) → D (egress + DaemonSet pre-flight) → E (Enforce: blue first, then confirm green DaemonSet on all nodes).

**Phase A — sign + attest.** Run `monthly-docker-image-retag` (`workflow_dispatch`).
- *Pass test:* because the signer is cosign v2.x, a sibling legacy `sha256-<digest>.sig` tag appears in the Docker Hub repo. Confirm with `cosign tree technotuba/nginx@sha256:<digest>` (don't assume the tag name; the tree is authoritative). Then verify locally (public image, no auth):
  ```
  cosign verify technotuba/nginx@sha256:<digest> \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    --certificate-identity-regexp '^https://github\.com/gregheffner/cicd/\.github/workflows/build-stage-scan\.yaml@refs/heads/main$'
  ```
  Expect `Verified OK`.

**Phase B — CI verify gate + digest-pin.**
- *Pass test:* the signed digest passes; the `pin-and-commit` job writes `technotuba/nginx@sha256:…` + `imagePullPolicy: IfNotPresent` into both manifests; promotion proceeds.
- *Negative 1 (unsigned):* point verify at a throwaway unsigned image → cosign non-zero → commit job skipped.
- *Negative 2 (foreign-signed):* sign an image from a *different* repo/workflow, verify against your identity regexp → fails on identity mismatch (proves "signed by someone" ≠ "signed by MY workflow").
- *Self-revert check:* run `monthly-docker-image-retag` a second time and confirm the manifests still contain `@sha256` (not `:latest`/`:vYYYY.MM`) afterward — proves the `sed`→`yq` rewrite holds.

**Phase C+ — cut the live deployment onto a signed digest (the outage-prevention gate).** With B merged, let one full run pin blue+green to the signed digest and ArgoCD recreate the pods on it. Confirm `kubectl get pods -n prod -o jsonpath` shows every nginx container running `@sha256:<signed>`. **Do not proceed to Enforce until this is true.**

**Phase C — Kyverno Audit.** Apply the policy with `validationFailureAction: Audit`. Keep the soak **short — days, not 1-2 weeks** (Audit blocks nothing, so a live prod image is unenforced the whole time; scenarios "unsigned `:latest` push" and "direct unsigned-digest commit + selfHeal" are NOT mitigated until Enforce). Tests:
  ```
  kubectl get clusterpolicyreport,policyreport -A
  kubectl get policyreport -n prod -o yaml | grep -A3 verify-technotuba-nginx
  ```
  Confirm the nginx container shows `pass` and **fail2ban does not appear as a fail** (scope correct). If fail2ban fails, fix scope before Enforce.

**Phase D — pre-Enforce DaemonSet + egress pre-flight (green is a DaemonSet, not a Deployment).** Green runs one pod **per node**, so it cannot be staged behind a Service-selector switch the way the blue Deployment can — at Enforce, every node's green pod must pass admission immediately.
- Confirm Kyverno admission pods have working egress to `fulcio.sigstore.dev` + `rekor.sigstore.dev` from the cluster network.
- Confirm the Audit PolicyReport shows the green DaemonSet pod on **all 4 nodes** as `pass`.
- Confirm verification-result caching is on so transient Sigstore blips don't re-block already-verified digests on pod restart.

**Phase E — Enforce.** Flip `validationFailureAction: Audit → Enforce`, commit (selfHeal applies it). State plainly: **scenarios 1 (unsigned `:latest` push) and 4 (direct unsigned-digest commit) become mitigated only at this point.**
- *Enforcement works:* commit a manifest referencing an **unsigned** `technotuba/nginx` digest → admission rejects the pod; selfHeal can't resurrect it. Confirm the legit signed digest still admits.
- *fail2ban regression:* confirm the privileged fail2ban sidecar still admits (out of scope). If a pod is rejected for fail2ban, revert to Audit immediately (break-glass).
- *DaemonSet check:* confirm green DaemonSet pods still admit on every node; watch for per-node brownout / admission retry churn.
- *Blue/green switch:* run a switch and confirm both still admit (both in ns `prod`, both covered by scope).

---

## 6. RESIDUAL RISK

- **Window before Enforce (Phases A-D):** Audit blocks nothing. An unsigned `:latest` push or a direct unsigned-digest commit + selfHeal deploys unobstructed. Keep the soak to days, not weeks. **Not mitigated until Phase E.**
- **Repo write-access cap (the big one):** Kyverno + identity-pinning only hold if commit access to `main` and **tag creation** are controlled. Tag refs are dropped from the regexp; you still must protect tags (most branch-protection does NOT cover tags). Without §4.3, an attacker who can push a tag or commit a foreign-but-signed digest degrades enforcement. **This is a manual GitHub setting no YAML here can enforce.**
- **Fail-closed availability:** `failurePolicy: Fail` + Sigstore outage/egress break can block (re)admission of `technotuba/nginx*` pods (restarts, drains, HPA, selfHeal). Mitigated by 3 replicas, result caching, the Audit-rollback break-glass, and the optional `failurePolicy: Ignore` first window.
- **DaemonSet (green) cannot be staged:** per-node admission means a scope/signature/egress fault hits every node at once and the controller retries indefinitely. Phase D pre-flight is the guard.
- **Live unsigned image at cutover:** Phase C+ exists specifically to retire the pre-signing unsigned digest before Enforce; skipping it = guaranteed outage on next pod restart.
- **Rekor public-log exposure:** records digest/cert/workflow-identity/timestamp — all already public for this repo/image. Non-issue here.
- **Signature-format interop:** mitigated by pinning the signer to cosign v2.x (legacy `.sig`) and confirming the Kyverno chart's cosign — but **must be re-verified** if you bump cosign to v3 or change the chart.

**Goal met:** zero packages on k8-primary. Signing + verify run on GitHub `ubuntu-latest`; `yq` for manifest edits runs there too; cluster verification runs inside Kyverno pods. The only cluster addition is the ArgoCD-managed Kyverno Application.

**Files to edit:** `/Volumes/vmshare/scripts/cicd/.github/workflows/monthly-docker-image-retag.yaml` (perms 10-11; push 77-78; sign+SBOM steps; replace sed steps 99-103 with `yq` digest-pin; add verify job), `/Volumes/vmshare/scripts/cicd/.github/workflows/update-blue-deployment-to-latest.yaml` (replace sed line 28 with `yq` digest-pin; add verify gate the commit `needs`), `/Volumes/vmshare/scripts/cicd/prod/nginx-blue.yaml` (line 37 → `@sha256`, line 38 → `IfNotPresent`, via the workflow), `/Volumes/vmshare/scripts/cicd/DR/nginx-green.yaml` (line 36 → `@sha256`, line 37 → `IfNotPresent`, via the workflow).
**Files to create:** `/Volumes/vmshare/scripts/cicd/security/verify-nginx-signature.yaml`, `/Volumes/vmshare/scripts/cicd/security/kyverno-app.yaml`.
**Belt-and-suspenders (optional):** `/Volumes/vmshare/scripts/cicd/.github/workflows/switch-traffic-to-blue.yaml` and `switch-traffic-to-green.yaml` (cheap verify-the-pinned-digest sanity check).

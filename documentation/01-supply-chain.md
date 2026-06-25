# Building and trusting the image
> Why this cluster builds its own nginx image, scans it before publish, signs it without a long-lived key, and re-checks that signature at three independent points.

## The problem
Running a public `nginx:latest` outsources three risks you can't see:
- **Base-image CVEs.** Upstream images lag patched OS packages; you inherit whatever was current when the maintainer last rebuilt.
- **Mutable-tag drift.** `:latest` (or any floating tag) can be re-pointed at new bytes after you tested it. A pod restart silently pulls something you never reviewed.
- **A signature alone proves nothing.** A valid signature only proves *signed-by-someone*. Without pinning *who*, an attacker who can sign with any Fulcio identity passes a naive `cosign verify`.

## What we do
**Build a fresh image weekly, from a pinned base.** [generate_dockerfile.py](../.github/scripts/generate_dockerfile.py) selects the newest nginx with an **even minor** (the stable branch — odd minors are mainline; comment at lines 41-43), then resolves that floating tag to an immutable `nginx@sha256:...` so "a poisoned upstream RE-TAG of the same name cannot silently enter between builds." `RUN apk upgrade` pulls current Alpine fixes "even when the upstream base lags" (the comment cites an OpenSSL `libssl3/libcrypto3` `.so` bump). Weekly, not daily: each build must clear a 72h soak before it can promote, so a faster cadence would only stack unpromoted candidates.

**Gate on Trivy before anything is published.** [build-stage-scan.yaml](../.github/workflows/build-stage-scan.yaml) builds single-arch, `load: true` (no push), then runs Trivy with `severity: HIGH,CRITICAL`, `ignore-unfixed: true`, `exit-code: 1` — fail the build on any *fixable* HIGH/CRITICAL. Only then does it push **one immutable `vYYYY.MM.DD` tag** and explicitly never `:latest`.

**Sign keyless, then pin the identity.** `cosign sign --yes` uses GitHub OIDC (`permissions: id-token: write`) for a short-lived Fulcio cert logged in Rekor — no long-lived key to leak. Verification pins **both** the OIDC issuer (`token.actions.githubusercontent.com`) **and** an anchored, dot-escaped identity regexp matching exactly this workflow file `@refs/heads/main`.

**Verify the same identity three times:** in CI right after signing; again at promote (re-verify before the Service flip — see [02-promotion-pipeline.md](02-soak-gate.md)); and at admission. [verify-nginx-signature.yaml](../security/verify-nginx-signature.yaml) is a Kyverno `ClusterPolicy` (`failureAction: Enforce`, `failurePolicy: Fail`, `required: true`, `ignoreSCT: false`) scoped to our nginx image only — fail2ban and every other image fall through unverified — with the identical issuer + `subjectRegExp`. It is installed via [heffner-security-app.yaml](../security/heffner-security-app.yaml) after the Kyverno controller ([kyverno-app.yaml](../security/kyverno-app.yaml), 3 replicas, 60m image-verify cache).

**Refuse `:latest` at the source.** The workflow's P0 gate aborts the whole build if either [prod/nginx-blue.yaml](../prod/nginx-blue.yaml) or [DR/nginx-green.yaml](../DR/nginx-green.yaml) still references a mutable tag; live manifests carry only `@sha256` pins with `imagePullPolicy: IfNotPresent`.

## Why this way
The obvious alternative — `nginx:latest` + "we'll patch when we notice" — trades security for convenience: you can't prove what's running and you can't stop drift. Keyless signing avoids the usual key-management footgun (a signing key that lives in CI secrets *is* the attack surface). But signing alone is theater unless you pin identity, so the issuer **and** the anchored regexp are repeated verbatim in CI and in the Kyverno policy — three checks that all reference one source of truth. The cost: Sigstore/Fulcio/Rekor become a build- and admission-time dependency. That's bounded deliberately — `imageVerifyCache` (60m) means a Sigstore outage only blocks *fresh, never-verified* digests, never already-running pods.

## If you're building your own
- **Pin the base by digest, not tag** — and resolve the digest in CI so each rebuild is reproducible and re-tag-poison-proof.
- **Scan before you push.** A gate that runs after publish has already leaked the artifact; build-and-load locally, scan, then push.
- **Keyless > long-lived keys**, but only if you pin *issuer + identity*. An unpinned `cosign verify` accepts anyone.
- **Verify at admission, not just in CI.** CI proves the build was clean; the Kyverno webhook proves *what actually schedules* matches — close the gap between "we built it right" and "only that runs."

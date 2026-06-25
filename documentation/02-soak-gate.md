# Why a 72-hour soak before going live

> A freshly built, scanned, and signed nginx image waits 72 hours on the standby color before it is allowed to serve a single request.

## The problem

"It scanned clean" is a statement about one moment. A CVE database is always behind reality: a vulnerability — or a deliberately poisoned package — can be disclosed *days after* an image passes its build-time scan. A subtly broken build (bad config, missing file, a regression that readiness probes don't catch) is similarly invisible at build time. Ship straight to production on green light and every one of those lands on live traffic before anyone knows.

So the question is: how do you buy time for the world to learn an image is bad, *and* re-check it against fresh knowledge right before it goes live — without a human babysitting the rollout?

## What we do

Two workflows, split across a mandatory delay. [build-stage-scan.yaml](../.github/workflows/build-stage-scan.yaml) builds the patched image, gates on Trivy (fail on fixable HIGH/CRITICAL), pushes an immutable `vYYYY.MM.DD` tag, cosign-signs it keyless, pins it onto the **standby** color manifest, and writes the [candidate.json](../.github/state/candidate.json) ledger as `state: soaking`. It does **no** cluster mutation. The 72h clock (`SOAK_SECONDS: 259200`) starts here: `promote_eligible_after_epoch = build_epoch + 72h`.

[soak-gate-promote.yaml](../.github/workflows/soak-gate-promote.yaml) runs **daily at 07:30 UTC** and is a deliberate no-op until eligible — the soak-time gate exits `go=false` while `now < eligible`. Once the clock is met it runs the real gates on the *exact staged digest*:

- **Fresh-DB Trivy re-scan** (`--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1`) — catches CVEs disclosed *during* the soak.
- **cosign re-verify** against the pinned signer identity (the build workflow on `refs/heads/main`) — confirms the signature still validates right before the flip.
- Drift, prereq refuse-gates (P0–P3, P5), and a standby-health gate — including that the standby pods are actually *serving the staged digest* (`imageID` match) with at least one Ready endpoint.

Then the **atomic flip**: the Service selector (in [shared/nginx-service.yaml](../shared/nginx-service.yaml)) and `state: promoting` are committed together in **one commit**, so the cluster's source of truth and the ledger can never disagree. A post-flip smoke test hits the ClusterIP; on any non-200 it **rolls back via git** and verifies the rollback actually landed on `origin/main` before exiting.

## Why this way

The naive alternative — scan once at build, deploy immediately — optimizes for speed at the cost of a multi-day blind spot. The soak trades ~72.5h of latency (Mon 07:00 build → eligible Thu 07:00 → first flip Thu 07:30) for two guarantees: an image bad enough to be disclosed within three days never reaches live, and the *re-scan at promote time* checks it against a database 72h smarter than the one that first approved it. Speed isn't free here, but for a static site behind a Cloudflare cache, three days of patch latency is cheap; a poisoned image on live is not.

Every gate **fails closed** — any uncertainty resolves to "no flip", never "flip anyway". The single atomic flip commit makes promotion idempotent and resumable: if the runner dies mid-promote, the next daily run sees `state: promoting` and *finishes the follow-ups* rather than flipping twice. And the shared `nginx-pipeline` concurrency group (`cancel-in-progress: false`) serializes build, promote, and pod-delete so they can never interleave at the cluster.

## If you're building your own

- **A clean scan is perishable.** Re-scan the exact digest against a fresh DB immediately before going live, not just at build.
- **Make the delay structural, not manual.** Encode the clock in a committed ledger; let a scheduled job no-op until it's met. No human gate to forget.
- **Flip atomically.** Commit the live-traffic pointer and the lifecycle state in one commit so a crashed run resumes instead of corrupting. Verify rollbacks landed — don't trust the retry loop.
- **Fail closed everywhere.** A gate that can't prove "safe" must default to "don't ship".

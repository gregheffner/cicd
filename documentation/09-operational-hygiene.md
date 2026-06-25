# Keeping it lean and least-privilege
> Why the housekeeping automation looks paranoid: it can delete the wrong thing or hand out the wrong power, so it is built to do neither.

## The problem
Routine maintenance is where quiet outages come from. Three failure modes drive every choice here:

- **Reaping a digest you still need.** Every signed image has a sibling Docker Hub tag `sha256-<digest>.sig`. Kyverno admits images by verifying that signature at admission (Enforce / `failurePolicy: Fail`). Delete the `.sig` of a digest that can still be admitted — a pod restart, a node reboot, a `git revert` — and pods stop being admitted. Delete the last plain tag of a live digest and you get an unscannable orphan; the comment in [prune-registry-tags.yaml](../.github/workflows/prune-registry-tags.yaml) calls that "the exact bug that already happened once."
- **Maintenance racing a deploy.** A prune that runs mid-build sees a half-pushed tag set; a manual pod-bounce mid-flip can kill the candidate.
- **Over-broad blast radius.** A workflow token with write scopes, or an "all namespaces" sweep, turns a cleanup into an incident.

## What we do
- **Prune by set logic, never by tag age.** [prune-registry-tags.yaml](../.github/workflows/prune-registry-tags.yaml) reads [prod/nginx-blue.yaml](../prod/nginx-blue.yaml), [DR/nginx-green.yaml](../DR/nginx-green.yaml) and `.github/state/candidate.json` from `origin/main` every run. It keeps a `HEAD` set (live blue/green pins + a soaking/promoting candidate) and a `BASE` overlay that adds every digest pinned in the last `ROLLBACK_WEEKS` (default 8) of git history. A `.sig` is reaped only in lockstep, after its digest is provably retired, and a live digest with no kept plain tag is a hard fail. A config assert refuses to run if the retention window (default 3 months) is shorter than the rollback horizon.
- **Manual prune is dry-run.** A `workflow_dispatch` prints the plan and deletes nothing unless `apply=true`; only the scheduled Mon 08:00 UTC cron applies — one hour after the Mon 07:00 build so the week's new tag is already committed.
- **One concurrency lane.** Build, promote, prune and [delete-kubernetes-pods.yaml](../.github/workflows/delete-kubernetes-pods.yaml) all share `concurrency.group: nginx-pipeline` with `cancel-in-progress: false`, so they serialise and never interleave (see [root README](../README.md)).
- **Least-privilege tokens.** Token-less workflows declare `permissions: {}` ([clear-cloudflare-cache.yaml](../.github/workflows/clear-cloudflare-cache.yaml), [log-rotate.yaml](../.github/workflows/log-rotate.yaml), [push-cloudflare-credentials.yaml](../.github/workflows/push-cloudflare-credentials.yaml), delete-kubernetes-pods.yaml). Prune gets only `contents: read`.
- **Destructive sweeps skip the dangerous namespaces.** The `all` option in delete-kubernetes-pods.yaml loops `kubernetes-dashboard default radar kube-system kube-flannel` and deliberately excludes `prod` (deleting both colors at once = outage) and `automation` (bouncing Argo CD stalls reconciliation). You must name those explicitly.
- **Secrets pushed in, never committed.** push-cloudflare-credentials.yaml creates the `cloudflare-creds` Secret in `prod` from repo secrets via `--dry-run=client -o yaml | kubectl apply -f -` (idempotent create-or-update), so the values live only in GitHub secrets and the cluster, never in git history.
- **Cadence.** Sun 08:00 UTC rolls the cloudflared connectors to pull a fresh `:latest` and pick up upstream fixes ([cloudflared-weekly-update.yaml](../.github/workflows/cloudflared-weekly-update.yaml)); Sun 23:59 UTC purges the Cloudflare cache; log rotation (node log truncation) is manual-only.

## Why this way
The obvious alternative — "keep the last N tags by date" — is what caused the original outage: tag age says nothing about whether git can still revert to a digest. Reading the truth files live, and protecting `.sig` and plain tag in lockstep with the digest, makes the safety overlay a function of the actual deploy state, not a guess. Likewise, `permissions: {}` costs nothing because these jobs act through the self-hosted runner's own kubeconfig/ansible or a scoped API token — the `GITHUB_TOKEN` is pure attack surface, so it gets zero scopes.

## If you're building your own
- Tie retention to reachability, not to age — protect anything git can roll back to, plus its signature, and fail closed if you can't prove a digest is retired.
- Make manual runs of destructive jobs default to dry-run; let only the schedule apply.
- Put every workflow that mutates the same artifact in one `cancel-in-progress: false` concurrency group.
- Default `permissions: {}` and add back only the scope a job truly needs; exclude prod and your GitOps controller from any "all" sweep.

# Declarative GitOps with Argo CD

> Git's `main` is the single source of truth: Argo CD reconciles the cluster, and the image-delivery pipeline promotes by committing — not by running `kubectl` against a workload.

## The problem

If delivery imperatively edits the cluster (`kubectl set image`, `kubectl patch`), live state drifts from git the moment a job runs: no audit trail, no atomic rollback, and a half-finished pipeline can leave the cluster in a state no file describes. You also get ordering races — apply a Deployment whose probe hits a config key before the ConfigMap exists, and pods crash-loop on first boot.

## What we do

Argo CD continuously reconciles against `main`; the image workflows "never touch the cluster imperatively — they commit to `main`" ([root README](../README.md)). Even a blue/green promotion is a commit: a one-line change to the Service `version` selector plus the `candidate.json` ledger, landed atomically, after which [soak-gate-promote.yaml](../.github/workflows/soak-gate-promote.yaml) triggers a force-sync rather than editing pods. (A few housekeeping workflows — rollout restart, pod delete — do touch the cluster, but they never change desired state; selfHeal restores it.)

Every Application runs `automated: { prune: true, selfHeal: true }` — see [cloudflared/cloudflared-app.yaml](../cloudflared/cloudflared-app.yaml). selfHeal reverts manual edits; prune deletes resources dropped from git. The bootstrap Application CRs are deliberately **self-excluding**: cloudflared's `directory.include` allowlists only its 5 workload files and omits the app YAML, so prune never deletes the Application that drives it, and out-of-band credential Secrets carry no Argo labels so prune leaves them alone.

Concerns split into separate Applications, each owning one path (README app table): `heffner-prod` (`prod/`), `heffner-dr` (`DR/`), `shared-services` (`shared/` — Service, HPAs, PDBs, nginx config), `cloudflared`, `radar`, `heffner-security`, `kyverno`. The boundary is the **path**, not the namespace — `heffner-prod`, `heffner-dr`, and `shared-services` all deploy into `prod` but own disjoint files. A bad sync in one app can't roll back another; the cost is more Application CRs and more one-time bootstrap surface.

## Why this way

**Synced is not Healthy.** Argo reports *Synced* when git == cluster, which says nothing about whether pods are Ready. So "health is checked independently before any flip" (README, *Reconciliation vs. readiness*) — the promote gate verifies pods are serving, not just that the manifest applied.

**Ordering is mostly absent — on purpose.** The prod/DR/shared apps use **no sync-wave** and converge in parallel. Only two apps order themselves: [datadog/datadog-agent-app.yaml](../datadog/datadog-agent-app.yaml) and [security/heffner-security-app.yaml](../security/heffner-security-app.yaml) carry `sync-wave: "1"` so the CR applies after its operator/CRD (wave 0), with `retry` backoff to "tolerate CRD-not-ready / webhook-not-ready races." That's the minimum coupling needed; everything else stays order-free.

## The operational lesson: land config first

`nginx.conf` is a ConfigMap ([shared/nginx-config.yaml](../shared/nginx-config.yaml)) mounted into the Deployment ([prod/nginx-blue.yaml](../prod/nginx-blue.yaml)) via `subPath` — there is **no Reloader**, so the file only takes effect when a pod restarts. The ConfigMap lives in `shared-services` and the Deployment in `heffner-prod`/`heffner-dr`, which sync independently with no wave between them.

So when a change couples the two — the live `/healthz` endpoint in `nginx.conf` *and* the liveness probe that hits it — the config must merge to `main` **before** the Deployment change. Land them together (or Deployment-first) and a new pod boots a probe pointing at config the other app hasn't synced yet, and fails readiness. Commit config first, let it sync, then ship the Deployment.

## If you're building your own

- **Make every change a commit.** If delivery mutates the cluster directly, you lose your audit log and your rollback. A promotion should be a diff, not a command.
- **One Application per concern, scoped paths.** `directory.include` allowlists keep an app from managing siblings — and from pruning itself.
- **Gate on Healthy, not Synced.** Sync only proves the YAML applied; check pod readiness separately before shifting traffic.
- **Without sync-waves or a Reloader, ordering is yours.** Land the ConfigMap before the workload that probes it.

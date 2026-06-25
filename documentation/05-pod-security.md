# Hardening the pods
> Why the nginx pods drop nearly every privilege, so a web-server compromise is a dead end rather than a foothold into the cluster.

## The problem
A public web server is the most-attacked process you run. If an attacker pops the nginx worker, the default container posture hands them a lot: root inside the container, a writable filesystem to drop a payload into, Linux capabilities to escalate with, and an auto-mounted service-account token that talks to the Kubernetes API. The goal is to make a compromised nginx worth almost nothing.

## What we do
The nginx container in [prod/nginx-blue.yaml](../prod/nginx-blue.yaml) and [DR/nginx-green.yaml](../DR/nginx-green.yaml) carries a `securityContext` commented "harden: non-root, read-only rootfs, no caps (listeners now >=1024)":
- `runAsNonRoot: true`, `runAsUser: 101`, `runAsGroup: 101` — never root. Listeners moved to ports 8080/8081, so `NET_BIND_SERVICE` (needed only for ports <1024) can go too.
- `readOnlyRootFilesystem: true` — the rootfs is immutable at runtime. Only the three paths nginx genuinely writes are carved out as `emptyDir`: `/var/cache/nginx`, `/var/run` (the pid file), and `/tmp`. The volume comment notes emptyDir is 0777, so uid 101 writes with no `fsGroup` needed.
- `allowPrivilegeEscalation: false` and `capabilities: drop: [ALL]` — no setuid path back to root, no kernel privileges at all.
- `automountServiceAccountToken: false` — commented "rec #4: static web server never calls the k8s API." No token to steal means no cluster pivot. Both colors run under a **dedicated `nginx` ServiceAccount with zero RoleBindings** ([shared/nginx-sa.yaml](../shared/nginx-sa.yaml)) instead of sharing the namespace `prod` SA — so even a leaked token grants nothing, and that SA re-asserts `automountServiceAccountToken: false` as defense-in-depth in case a pod spec ever regresses.

The fail2ban native sidecar is deprivileged the same way (`allowPrivilegeEscalation: false`, drop ALL) because, per its comment, "bans go via the Cloudflare API, not local iptables" — so it never needs `NET_ADMIN` to write firewall rules.

## Why this way
The teaching bit is the **fail2ban native sidecar**. The inline outage review (2026-06-25) records that as a plain sidecar with no readinessProbe, a fail2ban OOM/liveness restart flipped `Pod.Ready=false` and ejected the *healthy* nginx from the Service — correlated across all 3 pods, that meant 0 endpoints and an outage. The fix: run fail2ban as a restartable `initContainer` with `restartPolicy: Always` (a native sidecar, k8s >=1.29). A native sidecar's restarts no longer gate `Pod.Ready`, so a flapping ban-engine can never pull nginx out of `nginx-service`. The obvious alternative — adding a readinessProbe to a plain sidecar — would still couple two lifecycles that should be independent.

## If you're building your own
- Treat the rootfs as immutable: set `readOnlyRootFilesystem: true`, then add back *only* the dirs the process writes (here cache/run/tmp), each a tiny `emptyDir`. Move listeners above 1024 so you can drop `NET_BIND_SERVICE`.
- If a workload never calls the API, set `automountServiceAccountToken: false` **and** give it a dedicated ServiceAccount with no RoleBindings — never let it inherit the default namespace SA, which other workloads may have granted permissions to.
- Drop ALL capabilities and `allowPrivilegeEscalation: false` by default; add back only what actually fails. Most things need nothing.
- A sidecar whose health shouldn't affect traffic belongs in a native sidecar (`initContainer` + `restartPolicy: Always`), not as a regular container that votes on Pod readiness.

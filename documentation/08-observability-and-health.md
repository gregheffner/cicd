# Knowing it is healthy
> How the cluster proves nginx is alive, serving, and unbreached — without opening an extra public port or paying to log every request.

## The problem
A static-site pod can fail in three independent ways, and conflating them is dangerous:
- nginx the process wedges → needs a **restart**;
- nginx is up but its content/config mount is broken → needs **ejection from the Service**, not a restart;
- someone is probing or abusing the site → needs a **ban plus a metric**.

You also want live request metrics, but the obvious way to expose them — nginx `stub_status` on a public port — hands an attacker a free recon endpoint.

## What we do
**Metrics are scraped on the pod IP, never the Service.** `stub_status` listens on :8081 in [shared/nginx-config.yaml](../shared/nginx-config.yaml) (`status.conf`: `location /nginx_status { stub_status; access_log off; }`). The Datadog agent finds it by container autodiscovery: the pod annotation in [prod/nginx-blue.yaml](../prod/nginx-blue.yaml) sets `nginx_status_url: http://%%host%%:8081/nginx_status/`, where `%%host%%` resolves to the pod IP. Because the scrape is east-west to the pod, the `:81 → nginx-status` port is dropped from the public LoadBalancer in [shared/nginx-service.yaml](../shared/nginx-service.yaml). Datadog still gets metrics; the internet gets nothing.

**Liveness and readiness probe different things.** The readinessProbe hits `/` on :8080; the livenessProbe hits `/healthz` on :8080. `/healthz` is a tiny block in nginx.conf (`access_log off; return 200 "ok"`) — no disk read, no log line. So readiness asks "can I serve real content?" (broken mount → pulled from the Service) and liveness asks "is nginx itself answering?" (process wedged → restart).

**One log format serves security and observability.** `log_format fail2ban` records `$real_client_ip` (taken from `X-Forwarded-For`, since Cloudflare and the in-cluster cloudflared front the pod) and writes to both `/var/log/nginx/access.log` (read by the fail2ban native sidecar) and `/dev/stdout` (collected by Datadog via `containerCollectAll` in [datadog/datadog-agent.yaml](../datadog/datadog-agent.yaml) plus the `ad.datadoghq.com/nginx.logs` annotation). One line, two consumers.

**No version leak.** `server_tokens off;` in the http block — inherited into `status.conf` — keeps the nginx version out of response headers and error pages.

## Why this way
The probe split is the subtle part. Liveness *kills* the container; if it pointed at `/` and the content configMap were missing or synced late, every pod would CrashLoop forever instead of sitting quietly NotReady. So liveness must target only state guaranteed present the instant nginx.conf loads — and `/healthz` lives inside that same config, not a separate resource Argo CD might reconcile later. That is the GitOps-ordering lesson applied to health checks: never gate "restart" on something that can arrive out of order. Readiness, which only *ejects from the Service*, is the right place to be strict about content.

Scraping the pod IP costs nothing extra (the agent already runs as a DaemonSet on every node) and lets the public surface shrink to exactly one port (:80). The alternative — keeping :81 on the LoadBalancer so a central scraper can reach it — would publish a recon endpoint to save zero work.

## If you're building your own
- Expose internal metrics on the pod, scraped node-local; never widen a public Service just for monitoring.
- Make liveness cheap, logless, and dependent only on always-present config; make readiness test the real thing.
- Pick a log format both your log-ban tool (fail2ban) and your APM can parse, and ship it to a file *and* stdout — one signal, two consumers.
- Turn off version banners (`server_tokens off`); recon is free for attackers, so deny it by default.

# Banning abuse at the edge

> Why fail2ban reads the nginx access log but blocks attackers at the Cloudflare edge instead of in local iptables — and why it had to learn to ignore our own monitoring.

## The problem

A public static site gets a steady drizzle of 404-floods and scanners probing for `/wp-login.php`-style paths. Blocking them with local `iptables` means three failures at once: every node keeps its own ban list (3 replicas, one pod per node = three disjoint tables), the rule only fires *after* the request already crossed Cloudflare and hit nginx, and the ban container needs `NET_ADMIN` to edit the kernel firewall — a privileged sidecar guarding a static web server.

## What we do

fail2ban runs as a sidecar next to nginx in [prod/nginx-blue.yaml](../prod/nginx-blue.yaml) (the standby green color mirrors this spec), tailing the shared `/var/log/nginx/access.log`. Two jails in [shared/fail2ban-config.yaml](../shared/fail2ban-config.yaml):

- `nginx-404` — `maxretry = 10` over `findtime = 86400` (24h), `bantime = 2592000` (30 days). `failregex = ^<HOST> - .*\s"[^"]*" 404`.
- `nginx-botsearch` — `maxretry = 2` over 600s, 24h ban.

The ban **action** is not iptables. It invokes the script in [shared/fail2ban-cloudflare-ban-script.yaml](../shared/fail2ban-cloudflare-ban-script.yaml), which `POST`s a `mode: block` IP rule to the Cloudflare `firewall/access_rules` API (tagging the offender's ASN in the note). The block lands at Cloudflare's edge, so abusive IPs are dropped *before* they reach a node. Because the action is just an HTTPS call, the sidecar's `securityContext` drops **ALL** capabilities and sets `allowPrivilegeEscalation: false` — no `NET_ADMIN`. The inline comment says it plainly: "deprivileged — bans go via the Cloudflare API, not local iptables."

fail2ban removes *repeat* offenders (a ban after N hits); it pairs with an in-pod `limit_req` backstop that caps *burst* abuse in real time — see [caching and rate-limiting](10-caching-and-rate-limits.md). The two layer cleanly: the rate limit blunts a flood instantly, fail2ban evicts the source at the edge.

## Ignore your own infrastructure (the real lesson)

Two `ignore` layers in `jail.local` exist because of self-inflicted bans:

- `ignoreip` covers loopback, the flannel pod network, the service network, and the LAN/nodes. With the Service on `externalTrafficPolicy: Cluster`, cross-node traffic is SNAT'd and logs an internal address, not the real client — banning those would blackhole legitimate traffic.
- `ignorecommand = is-dd-synthetic.sh` skips Datadog uptime synthetics. A 404 on the `/image` path once let the `nginx-404` jail flag the cluster's *own* uptime monitor as an attacker. The synthetic uses a real browser UA (so a UA regex can't catch it) and rotates across cloud IPs, so the script fetches Datadog's published ranges, caches them 24h, and **fails closed** (any lookup error => do not ignore). The durable fix was two-part: ignore the monitor *and* fix the 404 source.

## Native sidecar, so a ban-engine crash never takes down the site

fail2ban is a **native sidecar** — an `initContainer` with `restartPolicy: Always` (needs k8s >=1.29). The comment records why: in an earlier outage a plain sidecar with no readiness gate OOM-restarted, flipped `Pod.Ready=false`, and ejected the *healthy* nginx from the Service across all 3 pods (0 endpoints = total outage). As a native sidecar its restarts no longer gate `Pod.Ready`. Its memory limit was raised 128->256Mi after that review (live RSS ~34Mi).

The public "Cloudflare blocks" badge is refreshed daily (cron `0 9 * * *`) by [update-cloudflare-block-badge.yaml](../.github/workflows/update-cloudflare-block-badge.yaml), which counts active edge IP block rules and `sed`-patches the [root README](../README.md) badge.

## If you're building your own

- **Ban at the edge, not the box.** Calling your CDN's firewall API drops attackers before they touch your origin and lets the ban engine run fully deprivileged (no `NET_ADMIN`, one shared ban list instead of one per node).
- **Whitelist your own plumbing first.** SNAT, health checks, and synthetic monitors all look like one noisy client; an over-eager jail will ban your own uptime probes.
- **A self-ban usually has two causes — fix both.** Add the ignore rule *and* the broken endpoint that triggered it.
- **A security sidecar must never gate the app's readiness.** Make it a native sidecar so its crashes can't pull a healthy service out of rotation.

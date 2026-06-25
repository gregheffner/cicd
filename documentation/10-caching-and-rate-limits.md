# Caching and rate-limiting behind the CDN

> Why HTML and static assets are cached differently, and why the origin rate-limits itself even though Cloudflare already does.

## The problem

Sitting behind a CDN invites two quiet mistakes:

- **Caching HTML as aggressively as assets.** If a page is told to cache for a year, a content change is invisible until that TTL expires — *unless* you remember to purge the edge on every deploy. The purge becomes load-bearing: miss one, or have it fail, and visitors get stale HTML for a long time.
- **Assuming the CDN is your only rate limiter.** Cloudflare caps abuse at the edge, but the origin behind the tunnel has no defense of its own if that boundary is ever bypassed or the origin is reached directly.

## What we do

Both controls live in [shared/nginx-config.yaml](../shared/nginx-config.yaml).

**Split the cache by volatility (rec #11).** Long-lived assets under `/image/` and `/images/` keep `max-age=31536000` (1 year). But `location /` — the HTML, which the comment notes is "un-fingerprinted and changes every deploy" — was *inheriting* that 1-year policy; it now sends `Cache-Control: public, max-age=300, must-revalidate`. HTML revalidates every 5 minutes; assets still cache for a year.

**Rate-limit at the origin as a backstop (rec #18).** `limit_req_zone` / `limit_conn_zone` keyed on `$real_client_ip` (the Cloudflare-supplied `X-Forwarded-For`, see [ingress](06-ingress.md)) — **30 r/s, burst 60 `nodelay`, 30 concurrent connections**, returning `429` on breach, applied to every location. The trivial `/healthz` probe and the Datadog status server (`:8081`, a separate server block) stay far under these.

## Why this way

**A short, must-revalidate HTML TTL makes the edge purge an optimization, not a correctness dependency.** With a 1-year HTML cache, the Cloudflare purge after each deploy *is* the publish step — forget it and stale HTML serves indefinitely. At `max-age=300, must-revalidate` a missed purge self-heals within minutes. Assets can safely cache for a year because their content is stable; only the HTML is volatile, so only the HTML pays the revalidation cost.

**Cloudflare is the primary limiter; the origin backstop is defense-in-depth.** It exists "if the tunnel IP leaks" (the comment's words). The key detail is the key: limiting on `$real_client_ip`, *not* the tunnel's source IP — every visitor arrives from the same in-cluster tunnel, so keying on the source would throttle all users as one bucket. The burst (60) is sized so a single real page pulling many assets isn't mistaken for a flood, while a sustained 30 r/s from one visitor still trips.

## If you're building your own

- **Cache fingerprinted assets forever, HTML briefly with `must-revalidate`.** Never let a cache purge become load-bearing for correctness — it's a speed-up, not your publish mechanism.
- **Rate-limit at the origin too, not just the CDN.** The CDN is primary; the origin is the backstop for the day the boundary leaks.
- **Key the limit on the real client IP** from your CDN's forwarded header — keying on the proxy's source IP lumps every visitor into one bucket and throttles them together.
- **Size the burst to a real page's asset count** so a legitimate multi-asset load doesn't get a `429`.

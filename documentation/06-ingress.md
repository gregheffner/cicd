# Edge and ingress with Cloudflare Tunnel

> Public traffic reaches the cluster through outbound-only Cloudflare Tunnels run as in-cluster Deployments, so no inbound port is ever opened and the origin IP is never exposed.

## The problem

A self-hosted origin behind a home or small-business LAN has three classic exposures:

- **Inbound ports.** Forwarding 80/443 to a node advertises the origin IP and hands every internet scanner a direct target — DDoS, exploit probes, and credential-stuffing all land on your hardware.
- **A single connector is a SPOF.** Running one tunnel daemon on one host means a node reboot or that one pod dying drops the entire public endpoint.
- **Losing the real client IP.** Behind a reverse proxy every request appears to come from the proxy, so rate-limiting and banning either do nothing or ban the proxy itself.

## What we do

cloudflared runs as a Kubernetes **Deployment, not a host daemon**. The connection is outbound: cloudflared dials Cloudflare's edge and traffic flows back down that tunnel, so **no inbound firewall port exists** and the node IP is never published. Each public app gets its own Deployment, tunnel, and credentials Secret — the static site and the radar app ([cloudflared/deployment-radar.yaml](../cloudflared/deployment-radar.yaml)) — so a leaked connector credential for one app cannot pivot to the other.

- **No SPOF:** [cloudflared/deployment-cloudflared.yaml](../cloudflared/deployment-cloudflared.yaml) runs `replicas: 3` with `requiredDuringScheduling` pod anti-affinity on `kubernetes.io/hostname` — one connector per worker node, so losing a node still leaves two live connectors and the endpoint stays up.
- **Routing is east-west over cluster DNS:** [cloudflared/configmap-cloudflared.yaml](../cloudflared/configmap-cloudflared.yaml) maps the public hostname to `http://nginx-service.prod.svc.cluster.local:80`, with a catch-all `http_status:404`. The tunnel hands off to the nginx Service by name, so the blue/green selector flip is transparent to the edge.
- **Connector is locked down:** `runAsNonRoot` uid/gid `65532`, `readOnlyRootFilesystem`, drop `ALL` caps, `allowPrivilegeEscalation: false`, `automountServiceAccountToken: false`, `--no-autoupdate`, config and tunnel credentials both mounted `readOnly`. Liveness `/healthcheck` and readiness `/ready` on the metrics port (`2000`).
- **Real client IP is recovered, then enforced:** [shared/nginx-config.yaml](../shared/nginx-config.yaml) sets `set_real_ip_from 0.0.0.0/0` + `real_ip_header X-Forwarded-For` + `real_ip_recursive on`, and a `log_format fail2ban` keyed on `$real_client_ip`. fail2ban acts on that address and pushes bans to the **Cloudflare API at the edge**, not local iptables — so the attacker is dropped before reaching the origin.

## Why this way

- **externalTrafficPolicy: Cluster, not Local.** The comment in [shared/nginx-service.yaml](../shared/nginx-service.yaml) states the tradeoff: ingress is now east-west (cloudflared → Service over cluster DNS), so ETP does not affect that path. `Cluster` stays the safe default — any node can route to a Ready pod (keeping the flip safe), and because the real client IP arrives in `X-Forwarded-For`, the SNAT that `Cluster` introduces does **not** break bans. `Local` would only matter if traffic entered via an external LoadBalancer, which it doesn't.
- **Trusting `0.0.0.0/0` is deliberate, not lazy.** nginx only ever receives connections from the in-cluster cloudflared pods, and Cloudflare overwrites `X-Forwarded-For` at its edge, so the last hop is trusted by construction. Trusting all upstreams is what lets `real_ip_recursive` pull the true client out of the chain.

## If you're building your own

- **Use an outbound tunnel before you ever port-forward.** It removes the origin IP and the inbound attack surface in one move — the highest-leverage edge decision.
- **Make the connector a multi-replica Deployment with per-node anti-affinity.** A tunnel is only as available as its connectors; one is a SPOF.
- **Give each app its own tunnel and credential**, so a single leaked connector secret can't reach your other services.
- **Recover the real client IP at the proxy and ban on *that*** — and prefer pushing bans to the edge API over local iptables, so abuse is dropped before it costs you a connection.

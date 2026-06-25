# Documentation — why it's built this way

> The decision-rationale companion to the [root README](../README.md). The root
> README explains **what** the cluster is and **how** it fits together; these pages
> explain **why** each major piece was built the way it was — the threat or
> inefficiency it addresses, and the tradeoff that was chosen over the obvious
> alternative.

Every page is grounded in the committed manifests and workflows (each claim links to
the file it comes from) and reflects the live cluster as of 2026-06-25 (Kubernetes
v1.32, three worker nodes). They are written for an engineer who wants to build their
own secure, efficient cluster and is trying to learn the *reasoning*, not just copy
the YAML. Read them in order or jump to a topic.

## Supply chain & delivery

| Page | The question it answers |
| --- | --- |
| [01 — Building and trusting the image](01-supply-chain.md) | Why build a custom nginx image, scan it before publish, sign it without a long-lived key, and verify that signature at three independent points. |
| [02 — Why a 72-hour soak](02-soak-gate.md) | Why a clean, signed image still waits 72 hours on standby — and is re-scanned against a fresher database — before it serves one request. |

## Availability & platform

| Page | The question it answers |
| --- | --- |
| [03 — Surviving node reboots and zero-downtime rollouts](03-high-availability.md) | Why blue/green plus required one-pod-per-node scheduling rides through node patching with no outage. |
| [04 — Declarative GitOps with Argo CD](04-gitops.md) | Why git is the only source of truth and CI never runs `kubectl` against workloads — and the cross-app sync-ordering lesson. |

## Runtime security

| Page | The question it answers |
| --- | --- |
| [05 — Hardening the pods](05-pod-security.md) | Why a web-server compromise is a dead end: non-root, read-only rootfs, no capabilities, no service-account token. |
| [06 — Edge and ingress with Cloudflare Tunnel](06-ingress.md) | Why public traffic arrives through outbound-only tunnels with no inbound ports and no exposed origin. |
| [07 — Banning abuse at the edge](07-intrusion-response.md) | Why abusive clients are blocked at the Cloudflare edge via fail2ban instead of local firewall rules. |
| [10 — Caching and rate-limiting behind the CDN](10-caching-and-rate-limits.md) | Why HTML and assets are cached differently, and why the origin rate-limits even though Cloudflare already does. |

## Operations

| Page | The question it answers |
| --- | --- |
| [08 — Knowing it is healthy](08-observability-and-health.md) | Why health is scraped from the pod, and the probe and port choices that follow from it. |
| [09 — Keeping it lean and least-privilege](09-operational-hygiene.md) | Why the housekeeping automation is deliberately paranoid and runs with the narrowest possible permissions. |

---

See also the [root README](../README.md) for the architecture overview and the
[codebase map](../CODEBASE_MAP.md) for a one-line tour of each folder. Operator
runbooks and anything that would expose internal topology are deliberately kept out
of this repository.

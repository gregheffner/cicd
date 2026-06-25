# Surviving node reboots and zero-downtime rollouts
> Why nginx runs as two interchangeable colors spread one-per-node, and why the scheduling rules are *required* rather than *preferred*.

## The problem
Two failure modes must be survived without ever serving zero endpoints:
- **Node patching.** A 3-node cluster (worker1/2/3) is rebooted for OS patching. A reboot empties a node's capacity. If pods were free to pile onto the two survivors, the returning node would come back idle and the fleet would stay lopsided.
- **Rollouts.** Shipping a new image or rolling back must not drop live traffic mid-flip — even though every node is already full.

## What we do
Two near-identical Deployments, **blue** ([prod/nginx-blue.yaml](../prod/nginx-blue.yaml)) and **green** ([DR/nginx-green.yaml](../DR/nginx-green.yaml)), each `replicas: 3`. Only one is live at a time: [shared/nginx-service.yaml](../shared/nginx-service.yaml) carries `selector.version: blue`, and promotion is a one-line flip of that label. The standby color soaks new images before it ever takes traffic (see [supply chain](01-supply-chain.md)).

Each color pins itself to the cluster shape:
- **`nodeAffinity` (required)** to `worker1/worker2/worker3` — that is where the images `hostPath` exists.
- **`podAntiAffinity` (required, not preferred)**, `topologyKey: kubernetes.io/hostname` against its own color label — *exactly one blue per node, one green per node.*
- **Rollout `maxSurge: 0 / maxUnavailable: 1`** — roll strictly one pod at a time.
- **HPA pinned `min=max=3`**, CPU `Utilization 70%` (in `nginx-service.yaml`).
- **Per-color PDB `maxUnavailable: 1`** ([shared/nginx-pdb.yaml](../shared/nginx-pdb.yaml)).

## Why this way
**Required anti-affinity, so a returned node refills itself.** The manifest comment says it plainly: *"a drained node's pod stays Pending until that node returns, then refills it."* With *preferred* affinity the scheduler would cram the displaced pod onto a survivor and the rebooted node would sit empty — you'd drift to 2 nodes carrying load. *Required* makes the only legal home for the third pod the node that left; the moment it rejoins, the Pending pod lands there. Spread becomes self-healing instead of best-effort.

**That required rule is exactly why surge-first is impossible.** The usual zero-downtime trick is `maxSurge>0`: add a new Ready pod before removing an old one. But every node already holds a pod of this color, so a surge pod has nowhere to schedule — it sits Pending forever. The manifest is explicit: *"required 1-per-node anti-affinity can't place a surge pod (every node full for this color)."* So we invert to `maxSurge: 0 / maxUnavailable: 1`: free one node, schedule the replacement there, repeat. You trade N+1 during a roll for N-1 one pod at a time — acceptable because two of three pods stay live and Cloudflare caches the static site.

**HPA pinned at 3 because real capacity is capped at 3.** One-per-node anti-affinity means a 4th replica has no node and would sit Pending. `min=max=3` makes the autoscaler honest; the `Utilization 70%` target still yields meaningful metrics (each container requests `cpu: 250m`, replacing an old `AverageValue` that never scaled) without ever asking for a pod that can't be placed. Both HPAs also carry `behavior.scaleDown.stabilizationWindowSeconds: 300` — anti-flap insurance that is *inert today* (nothing scales within a fixed `min==max==3`) and only matters if `maxReplicas` is ever raised above the node count; it's committed now so a future capacity bump doesn't thrash pods down on brief load dips.

**Per-color PDB caps voluntary disruption at one.** A `kubectl drain` for patching could otherwise evict more than one nginx at once. `maxUnavailable: 1` (deliberately *not* `minAvailable: 2`) holds the one-at-a-time guarantee and, per the file comment, *"never wedges a drain (it can't yield 0 allowed disruptions)."* Scope matters: PDBs gate **voluntary** evictions/drains only — not rolling updates or plain deletes.

## If you're building your own
- **Choose `required` anti-affinity when you want spread to *recover*, `preferred` when you only want it *usually*.** Required refills returning nodes — but it forecloses surge rollouts. Know that tradeoff going in.
- **`maxSurge` and one-pod-per-node are mutually exclusive.** If every node is full, surge-first can't schedule; use `maxSurge: 0 / maxUnavailable: 1` and lean on caching/redundancy to cover the missing pod.
- **Don't let HPA `max` exceed placeable capacity.** Anti-affinity × node count is the real ceiling; pin `min=max` when they're equal so the autoscaler never chases an unschedulable pod.
- **Blue/green is a label flip, not a redeploy.** Two Deployments behind one Service make promotion and rollback atomic and instant.
- **Prefer PDB `maxUnavailable: 1` over `minAvailable`.** It keeps the same guarantee across a scaling range and won't deadlock a drain.

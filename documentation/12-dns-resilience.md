# Surviving control-plane blips with a node-local DNS cache

> Why every node runs a local DNS cache in front of CoreDNS, so a control-plane reboot or a CoreDNS hiccup does not black-hole in-cluster name resolution.

## The problem

In-cluster name resolution is on the critical path for the public site. The cloudflared tunnel reaches nginx by resolving `nginx-service.prod.svc.cluster.local`, and that lookup goes through CoreDNS, whose Service endpoints are tracked by the API server. On a single control-plane cluster, a node reboot took down both a CoreDNS replica and the API server at the same time, so the dead CoreDNS endpoint was not removed from rotation. Roughly half of in-cluster DNS queries then timed out, and the public site returned 502 even though the nginx pods themselves were healthy. DNS was a single point of failure that could take the site down during routine patching.

## What we do

Every node runs a NodeLocal DNSCache agent as a DaemonSet ([nodelocaldns/daemonset.yaml](../nodelocaldns/daemonset.yaml)), listening on a link-local address, so pods resolve against a cache on their own node first. Its Corefile ([nodelocaldns/configmap.yaml](../nodelocaldns/configmap.yaml)) forwards the cluster zone to CoreDNS through a dedicated upstream Service ([nodelocaldns/service-upstream.yaml](../nodelocaldns/service-upstream.yaml)) and sends everything else to the configured upstream. The whole addon is reconciled from git ([nodelocaldns-app.yaml](../nodelocaldns/nodelocaldns-app.yaml), into `kube-system`), with Argo CD's default kind ordering applying the ServiceAccount, ConfigMap, and Service before the DaemonSet so the agent always finds its upstream at startup.

Alongside the cache, CoreDNS itself was scaled to three replicas with required anti-affinity, moved off the control-plane node, and given a PodDisruptionBudget. CoreDNS is a kubeadm-managed addon, so that change is applied out of band and is not stored in this repo.

## Why this way

**A local cache turns a hard dependency into a soft one.** When CoreDNS or the control plane has a brief outage, the node-local cache keeps answering from cached records instead of black-holing the query. The lookup that used to fail now succeeds from cache while the upstream recovers.

**It also reduces load and latency.** Most lookups are served from the node's own cache, so they never cross the network to CoreDNS, and CoreDNS sees far fewer queries.

**Spreading CoreDNS limits what a reboot can take down.** Three replicas, one per worker and none on the control-plane node, mean a node reboot loses at most one replica and the kube-dns Service still has healthy endpoints to answer from.

## If you're building your own

- **Treat DNS as critical-path infrastructure.** On a single control-plane cluster, CoreDNS reachability is tied to the API server, which makes it a single point of failure worth removing.
- **A node-local cache absorbs blips.** It is the difference between a transient upstream outage being invisible and it taking down anything that resolves a name.
- **Spread CoreDNS with required anti-affinity and a PDB, off the control-plane node.** Losing one node should never drop more than one replica.
- **Remember kubeadm addons reset on cluster upgrades.** A CoreDNS patch like this has to be re-applied after an upgrade.

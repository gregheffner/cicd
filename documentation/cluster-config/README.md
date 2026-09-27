# Cluster configuration reference (NOT applied by ArgoCD)

These files are **reference copies**, exported 2026-09-02. Nothing here is synced.
ArgoCD only watches: `DR`, `cloudflared`, `datadog`, `external-secrets`, `nodelocaldns`,
`prod`, `security`, `shared`, `weathermap`. `documentation/` is deliberately outside that
list.

## Why these are not GitOps-managed

`kubeadm-config` and `kubelet-config` in `kube-system` are owned by **kubeadm**, not by
ArgoCD. kubeadm reads and rewrites them during `kubeadm upgrade`. If ArgoCD also applied
them, the two would fight on every upgrade. So they are captured here for version history,
review and disaster recovery only.

## Why they are captured at all

Before this, every hardening change made in the 2026-09-02 CIS work existed **only in etcd**:
encryption at rest, API audit logging, the apiserver TLS and profiling flags, and the kubelet
serving-certificate settings. None of it was version controlled. A rebuild from scratch would
have silently lost all of it, and there was no diffable history of what changed or when.

## Files

| File | Live location | Applied by |
|---|---|---|
| `kubeadm-ClusterConfiguration.yaml` | `kube-system/kubeadm-config` ConfigMap, key `ClusterConfiguration` | kubeadm |
| `kubelet-KubeletConfiguration.yaml` | `kube-system/kubelet-config` ConfigMap, key `kubelet` | kubeadm, pulled per node by `kubeadm upgrade node phase kubelet-config` |
| `apiserver-audit-policy.yaml` | `/etc/kubernetes/audit/policy.yaml` on k8-primary | mounted into kube-apiserver |
| `kubelet-csr-approver-values.yaml` | Helm release `kubelet-csr-approver` in `kube-system` | Helm |

**Not captured, deliberately:** `/etc/kubernetes/enc/enc.yaml` contains live encryption keys.
It lives in 1Password (`Secure APIs`) in two items, the current secretbox key and the retired
aescbc key. The aescbc item must be kept: QNAP filesystem snapshots hold etcd dumps encrypted
with it.

## Re-applying after a rebuild

```bash
# apiserver flags + encryption + audit, from the ConfigMap:
kubectl -n kube-system edit cm kubeadm-config     # paste ClusterConfiguration
sudo kubeadm init phase control-plane apiserver --config <initcfg+clustercfg>.yaml

# kubelet settings, per node:
kubectl -n kube-system edit cm kubelet-config     # paste KubeletConfiguration
sudo kubeadm upgrade node phase kubelet-config && sudo systemctl restart kubelet
```

Verify no drift afterwards with `sudo kubeadm upgrade diff v<version>` — empty output means
the live manifests match what kubeadm would generate.

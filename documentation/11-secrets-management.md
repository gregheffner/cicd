# Secrets from 1Password, synced by External Secrets Operator

> 1Password is the source of truth for every external secret. The cluster holds only a synced copy, plus one bootstrap token that cannot come from 1Password itself.

## The problem

The external secrets the cluster needs (Cloudflare API tokens, the Datadog key, the OpenWeatherMap key, the two cloudflared tunnel credentials, the Argo CD repository token) used to live only inside the cluster's etcd. There was no copy of record anywhere else, so a rebuilt cluster meant re-entering every secret by hand. They were created with `kubectl create secret ... | kubectl apply`, which also left the secret value sitting in a `last-applied-configuration` annotation on the object. Committing them to git is not an option because the values are plaintext. So there was no source of truth, no rotation path, and no audit trail.

## What we do

External Secrets Operator runs in the cluster ([external-secrets/external-secrets-operator-app.yaml](../external-secrets/external-secrets-operator-app.yaml), Helm chart `2.7.0`, run with controller and webhook replicas for HA). It uses the 1Password service-account SDK provider, which talks to 1Password directly, so there is no Connect server to run or back up.

A dedicated, read and write 1Password service account is scoped to a single vault that holds only these cluster secrets. Its token is the one bootstrap Secret, `external-secrets/onepassword-token`, which is created out of band and never committed. A single [ClusterSecretStore](../external-secrets/clustersecretstore.yaml) names that vault and the namespaces allowed to read from it.

One `ExternalSecret` per secret maps a 1Password item and field to a native Kubernetes Secret of the same name, so the pods that consume it need no change: see [es-prod-cloudflare-creds.yaml](../external-secrets/es-prod-cloudflare-creds.yaml), [es-radar.yaml](../external-secrets/es-radar.yaml), [es-cloudflared-tunnel-creds.yaml](../external-secrets/es-cloudflared-tunnel-creds.yaml), [es-cloudflared-radar-creds.yaml](../external-secrets/es-cloudflared-radar-creds.yaml), [es-datadog-secret.yaml](../external-secrets/es-datadog-secret.yaml), and [es-automation-repo-1938946305.yaml](../external-secrets/es-automation-repo-1938946305.yaml) (which also re-applies the `argocd.argoproj.io/secret-type: repository` label so Argo CD still recognizes the credential). The store and all `ExternalSecret` files are reconciled by [external-secrets-config-app.yaml](../external-secrets/external-secrets-config-app.yaml). Because the service account also has write access, a `PushSecret` can send a secret generated inside the cluster back up to 1Password when that is needed.

## Why this way

**1Password is the source of truth, git holds only the wiring.** The `ExternalSecret` and store manifests describe where a secret comes from, never its value, so the whole setup is safe to commit.

**ESO is not a runtime single point of failure.** Once a Secret is written into etcd, the pods that mounted it keep running even if ESO or 1Password is unreachable. ESO only matters for first materialization, for propagating a rotation, and for recovering a Secret that was deleted.

**Owner plus prune-off, for self-healing without a foot-gun.** Each `ExternalSecret` uses `creationPolicy: Owner`, so the Secret is an exact mirror of 1Password and is recreated automatically if it is ever deleted. The config Application runs with prune turned off, so removing a file from git cannot cascade-delete a live Secret through the owner reference. The cost is that decommissioning a secret is a deliberate `kubectl delete`, not a file removal.

**A dedicated vault and account bound the blast radius.** ESO has its own service account and its own vault, separate from the account CI uses, so a leak of one token does not expose the other vault.

## The one secret that stays in the cluster

ESO needs a credential to reach 1Password before it can fetch anything, so the service-account token has to exist as a Kubernetes Secret. That bootstrap token is the single secret that cannot be sourced from 1Password. It is protected by etcd encryption at rest, kept out of git, and rotated by replacing the Secret.

## If you're building your own

- **Keep exactly one bootstrap credential out of band.** Everything else can flow from the vault; accept that the root token is the one thing you provision by hand.
- **Use the service-account SDK provider if you have a service account.** It avoids running and securing a separate Connect server.
- **Owner plus prune-off gives self-healing without the cascade-delete trap.** Keep the manifests in git permanently and decommission with an explicit delete.
- **Scope a dedicated vault and a dedicated account.** Least privilege here means a leaked token can only touch the one vault it was issued for.

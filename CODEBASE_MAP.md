# Codebase Map

GitOps repo for a blue/green Kubernetes nginx deployment, plus a few small apps. One line per top-level folder: what lives here. See `README.md` for the full architecture.

- `cloudflared/`: in-cluster HA Cloudflare Tunnels (two 3-replica Deployments) that expose `greg.heffner.live` and `radar.heffner.live`, plus the Argo CD app (`cloudflared`)
- `prod/`: blue color, `nginx-blue.yaml` — the `nginx-web-blue` **Deployment** (Argo CD app `heffner-prod`, namespace `prod`)
- `DR/`: green color, `nginx-green.yaml` — the `nginx-web-green` **Deployment**, the standby color (Argo CD app `heffner-dr`, also namespace `prod`)
- `shared/`: cluster-wide config for the nginx stack — the blue/green Service selector, HPAs, PDBs, `nginx.conf`, fail2ban configs, and `www-configmap.yaml` (the website content) (Argo CD app `shared-services`)
- `weathermap/`: `radar.yml` for the radar weather app (Argo CD app `radar`, namespace `radar`)
- `security/`: Kyverno install + the keyless-cosign `verify-technotuba-nginx` ClusterPolicy (Argo CD apps `kyverno` + `heffner-security`)
- `DockerImage/`: the `technotuba/nginx` build context — entrypoint scripts COPYed into the CI-generated, digest-pinned Dockerfile (no committed Dockerfile/nginx.conf; runtime config comes from `shared/nginx-config.yaml`)
- `.github/`: GitHub Actions workflows (blue/green build→soak→promote, Docker Hub tag prune, Cloudflare cache/badge, cloudflared rollout, pod cycling, log rotation) and helper scripts
- `archive/`: dormant manifests no longer deployed and not Argo CD-managed — `chat/`, `webapp/`, `web-search/`, `ip-search/`, `jax-help/`

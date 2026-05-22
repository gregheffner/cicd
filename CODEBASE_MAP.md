# Codebase Map

GitOps repo for a blue-green Kubernetes nginx deployment, plus manifests for a handful of small apps. One line per top-level folder: what lives here.

- `.github/`: GitHub Actions workflows that run the deploys (blue/green traffic switch, Cloudflare cache clear, pod cycling, monthly image retag) and helper scripts
- `DR/`: green environment manifest, `nginx-green.yaml`, a DaemonSet for disaster recovery
- `prod/`: blue environment manifest, `nginx-blue.yaml`, a StatefulSet for production
- `DockerImage/`: the `technotuba/nginx` container build (Dockerfile, `nginx.conf`, entrypoint)
- `shared/`: cluster-wide config: nginx, fail2ban, and `www-configmap.yaml` (the website content)
- `chat/`: Kubernetes manifest for the encrypted chat app
- `ip-search/`: Kubernetes manifest for the IP lookup app
- `web-search/`: Kubernetes manifest for the web search app
- `weathermap/`: Kubernetes manifest for the weather radar app
- `webapp/`: Kubernetes manifest for the Python web app
- `jax-help/`: manifests for side sites: an IT helpdesk and a pressure-washing page

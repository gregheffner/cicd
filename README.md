# cicd

This project demonstrates a blue-green deployment strategy with two separate deployments: **green** and **blue**.

## Deployment Details

- **Green Deployment**
  - Location: `DR/nginx-green.yaml`
  - Docker Image: [`technotuba/nginx:vYYYY.MM`](https://hub.docker.com/r/technotuba/nginx/tags)
  - Represents the new version to be released.
- **Blue Deployment**
  - Location: `prod/nginx-blue.yaml`
  - Version: `vlatest`
  - Docker Image: [`technotuba/nginx:latest`](https://hub.docker.com/r/technotuba/nginx/tags)
  - Represents the currently running production version.

The blue-green deployment approach allows seamless switching between versions, minimizing downtime and risk during updates. In this setup, the **DR (disaster recovery) environment** uses **DaemonSets** to ensure that the green version runs on every node, providing high availability for testing or backup purposes. The **PROD (production) environment** uses **StatefulSets** for the blue version, which is ideal for managing stable, persistent workloads in production. This separation ensures that updates can be tested safely in DR before promoting them to production, and that each environment uses the most appropriate Kubernetes controller for its needs.

## Environment Separation

> **Note:**  
> The green deployment manifests have been moved to the `DR` (disaster recovery) folder, and the blue deployment manifests are now in the `prod` (production) folder.

**Why separate environments?**
- **Clarity:** Keeping deployment files for different environments in separate folders makes it clear which resources belong to production and which are for disaster recovery or staging.
- **Ease of Use:** This structure simplifies automation, CI/CD workflows, and manual operations by reducing confusion and the risk of accidental changes to the wrong environment.
- **Clean Code:** Environment separation helps maintain a clean repository, making it easier to manage, review, and audit changes.

> **Important:**  
> After deploying a new version, don't forget to update the service to point to either the green or blue deployment as appropriate.

_Last changed: June 8, 2025_
# cicd

This project demonstrates a blue-green deployment strategy with two separate deployments: **green** and **blue**.

## Deployment Details

- **Green Deployment**
  - Version: `v2025.6.1`
  - Docker Image: [`technotuba/nginx:vYYYY.MM`](https://hub.docker.com/r/technotuba/nginx/tags)
  - Represents the new version to be released.
- **Blue Deployment**
  - Version: `v2025.10` (previous stable version)
  - Docker Image: [`technotuba/nginx:latest`](https://hub.docker.com/r/technotuba/nginx/tags)
  - Represents the currently running production version.

The blue-green deployment approach allows seamless switching between versions, minimizing downtime and risk during updates.

> **Important:**  
> After deploying a new version, don't forget to update the service to point to either the green or blue deployment as appropriate.

_Last changed: June 1, 2025_

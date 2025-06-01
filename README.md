# cicd

This project demonstrates a blue-green deployment strategy with two separate deployments: **green** and **blue**.

## Deployment Details

- **Green Deployment**
  - Version: `v2025.6.1`
  - Docker Image: [`technotuba/nginx:v2025.6.1`](https://hub.docker.com/layers/technotuba/nginx/v2025.6.1/images/sha256-eb05641f9ef6141e886329d8620e8fb75e36710b0c036ba325e4b3a241b64808)
  - Represents the new version to be released.
- **Blue Deployment**
  - Version: `v2025.5.10` (previous stable version)
  - Docker Image: [`technotuba/nginx:v2025.5.10`](https://hub.docker.com/layers/technotuba/nginx/v2025.5.10/images/sha256-75423cf5fa0181e58f1fea3736feec9954868addd5ef6190f3b316f951c479a4)
  - Represents the currently running production version.

The blue-green deployment approach allows seamless switching between versions, minimizing downtime and risk during updates.

> **Important:**  
> After deploying a new version, don't forget to update the service to point to either the green or blue deployment as appropriate.

_Last changed: June 1, 2025_
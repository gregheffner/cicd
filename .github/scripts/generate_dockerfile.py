import datetime
import os
import re

import requests
from packaging import version


def get_stable_nginx_version():
    # Allow override by environment variable
    override = os.environ.get("NGINX_VERSION")
    if override:
        print(f"[INFO] Using NGINX_VERSION override: {override}")
        return override

    print("[INFO] Fetching latest stable nginx version from Docker Hub...")
    tags = []
    url = "https://hub.docker.com/v2/repositories/library/nginx/tags/?page_size=100"
    while url:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            tags.extend([t["name"] for t in data["results"]])
            url = data.get("next")
        except Exception as e:
            print(f"[ERROR] Failed to fetch nginx tags from Docker Hub: {e}")
            break

    # Filter tags like '1.29.0-alpine-slim' or '1.29-alpine-slim'
    version_tags = []
    for tag in tags:
        m = re.match(r"^(\d+\.\d+(?:\.\d+)?)-alpine-slim$", tag)
        if m:
            version_tags.append(m.group(1))

    if not version_tags:
        print("[WARN] No version tags found, falling back to 1.28.0")
        return "1.28.0"

    # Sort and get the latest
    latest = max(version_tags, key=version.parse)
    print(f"[INFO] Latest stable nginx version found: {latest}")
    return latest


def get_latest_njs_version():
    url = "https://api.github.com/repos/nginx/njs/releases/latest"
    resp = requests.get(url)
    data = resp.json()
    return data["tag_name"].lstrip("v")


def get_latest_njs_debian_release():
    url = "https://sources.debian.org/api/src/njs/"
    resp = requests.get(url)
    data = resp.json()
    for version in data.get("versions", []):
        if "bookworm" in version["suites"]:
            ver = version["version"]
            if "-" in ver:
                upstream, debrel = ver.split("-", 1)
                return debrel, debrel
    return "3~bookworm", "1~bookworm"


NGINX_VERSION = get_stable_nginx_version()
NJS_VERSION = get_latest_njs_version()
NJS_RELEASE, PKG_RELEASE = get_latest_njs_debian_release()

base_image = f"nginx:{NGINX_VERSION}-alpine-slim"

dockerfile_content = f"""\

FROM {base_image}

LABEL description="Built by technotuba for K8s NGINX WWW"
LABEL maintainer="main.plan5783@fastmail.com"
LABEL ClusterAge="{datetime.datetime.now().strftime('%a %b %d %I:%M:%S %p %Z %Y')}"
LABEL source="https://github.com/gregheffner/k8-nginx-webpage"

ENV NGINX_VERSION={NGINX_VERSION} \
    NJS_VERSION={NJS_VERSION} \
    NJS_RELEASE={NJS_RELEASE} \
    PKG_RELEASE={PKG_RELEASE}

RUN set -x && apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/America/New_York /etc/localtime && \
    echo "America/New_York" > /etc/timezone && \
    chmod +x /docker-entrypoint.sh && \
    chmod +x /docker-entrypoint.d/*.sh || true

COPY DockerImage/docker-entrypoint.sh /
COPY DockerImage/docker-entrypoint.d/ /docker-entrypoint.d/

ENV TZ=America/New_York

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
"""

with open("Dockerfile", "w") as f:
    f.write(dockerfile_content)

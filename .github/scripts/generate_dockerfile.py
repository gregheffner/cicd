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

    # nginx STABLE branches have an EVEN minor (1.28, 1.30, ...); ODD is mainline
    # (1.29, 1.31). max() over all tags picks mainline despite this function's name,
    # so filter to the stable (even-minor) branch first.
    stable_tags = [v for v in version_tags if version.parse(v).release[1] % 2 == 0]
    if not stable_tags:
        print("[WARN] No even-minor stable tags; falling back to newest available.")
        stable_tags = version_tags
    latest = max(stable_tags, key=version.parse)
    print(f"[INFO] Latest STABLE nginx version found: {latest}")
    return latest


def get_latest_njs_version():
    url = "https://api.github.com/repos/nginx/njs/releases/latest"
    resp = requests.get(url, timeout=10)
    data = resp.json()
    return data["tag_name"].lstrip("v")


def get_latest_njs_debian_release():
    url = "https://sources.debian.org/api/src/njs/"
    resp = requests.get(url, timeout=10)
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


def resolve_base_digest(tag):
    """Resolve the floating base tag (e.g. nginx:1.30.3-alpine-slim) to an immutable
    library/nginx@sha256:... so the build is reproducible and a poisoned upstream
    RE-TAG of the same name cannot silently enter between builds. Falls back to the
    floating tag (with a warning) if the registry can't be reached."""
    repo = "library/nginx"
    try:
        token = requests.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
            timeout=10,
        ).json()["token"]
        r = requests.get(
            f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.docker.distribution.manifest.list.v2+json,"
                          "application/vnd.oci.image.index.v1+json,"
                          "application/vnd.docker.distribution.manifest.v2+json",
            },
            timeout=10,
        )
        r.raise_for_status()
        digest = r.headers["Docker-Content-Digest"]
        if not digest.startswith("sha256:"):
            raise RuntimeError(f"unexpected digest {digest!r}")
        print(f"[INFO] Base nginx:{tag} -> {digest}")
        return digest
    except Exception as e:
        print(f"[WARN] Could not resolve base digest for nginx:{tag} ({e}); "
              f"using floating tag (NOT reproducible).")
        return None


_base_tag = f"{NGINX_VERSION}-alpine-slim"
_base_digest = resolve_base_digest(_base_tag)
# Immutable @sha256 ref when resolvable; else the floating tag as a fallback.
base_image = f"nginx@{_base_digest}" if _base_digest else f"nginx:{_base_tag}"

dockerfile_content = f"""\

FROM {base_image}
# base tag at build time: nginx:{_base_tag}

LABEL description="Built by technotuba for K8s NGINX WWW"
LABEL maintainer="main.plan5783@fastmail.com"
LABEL ClusterAge="{datetime.datetime.now().strftime('%a %b %d %I:%M:%S %p %Z %Y')}"
LABEL source="https://github.com/gregheffner/k8-nginx-webpage"

ENV NGINX_VERSION={NGINX_VERSION} \
    NJS_VERSION={NJS_VERSION} \
    NJS_RELEASE={NJS_RELEASE} \
    PKG_RELEASE={PKG_RELEASE}
ENV TZ=America/New_York

# apk upgrade picks up patched OS packages from the Alpine repo (e.g. openssl
# libssl3/libcrypto3 3.5.6-r0 -> 3.5.7-r0, an ABI-stable .so swap) so the monthly
# rebuild ships current security fixes even when the upstream base lags.
RUN set -eux && \
    apk upgrade --no-cache && \
    apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/America/New_York /etc/localtime && \
    echo "America/New_York" > /etc/timezone

COPY DockerImage/docker-entrypoint.sh /
COPY DockerImage/docker-entrypoint.d/ /docker-entrypoint.d/

# chmod AFTER the COPY (the previous chmod-before-COPY ran against base-image files
# and was a no-op on the project scripts, silently masked by `|| true`).
RUN set -eux && \
    chmod +x /docker-entrypoint.sh && \
    chmod +x /docker-entrypoint.d/*.sh

EXPOSE 80

# rec #23: explicit STOPSIGNAL backstops the preStop "nginx -s quit" graceful drain even
# if the lifecycle hook is skipped. (The official nginx base already sets SIGQUIT; stated
# explicitly so a base-image change can't silently revert it to SIGTERM.)
STOPSIGNAL SIGQUIT

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
"""

with open("Dockerfile", "w") as f:
    f.write(dockerfile_content)

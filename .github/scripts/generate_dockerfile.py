import datetime
import requests
import re

def get_stable_nginx_version():
    url = "https://nginx.org/en/download.html"
    resp = requests.get(url)
    # Find the "Stable version" section and extract the first nginx-X.Y.Z occurrence after it
    stable_section = re.search(r"Stable version.*?nginx-(\d+\.\d+\.\d+)", resp.text, re.DOTALL)
    if stable_section:
        return stable_section.group(1)
    return "1.28.0"

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

ENV NGINX_VERSION={NGINX_VERSION} \\
    NJS_VERSION={NJS_VERSION} \\
    NJS_RELEASE={NJS_RELEASE} \\
    PKG_RELEASE={PKG_RELEASE}

RUN set -x && \\
    rm -rf /var/lib/apt/lists/*

RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/America/New_York /etc/localtime && \
    echo "America/New_York" > /etc/timezone
ENV TZ=America/New_York

COPY DockerImage/docker-entrypoint.sh /
COPY DockerImage/docker-entrypoint.d/ /docker-entrypoint.d/

RUN chmod +x /docker-entrypoint.sh && \\
    chmod +x /docker-entrypoint.d/*.sh || true

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
"""

with open("Dockerfile", "w") as f:
    f.write(dockerfile_content)

FROM nginx:1.28.0-alpine-slim

LABEL description="Built by technotuba for K8s NGINX WWW"
LABEL maintainer="main.plan5783@fastmail.com"
LABEL ClusterAge="Fri May 24 01:22:17 PM EDT 2024"
LABEL source="https://github.com/gregheffner/k8-nginx-webpage"

ENV NGINX_VERSION=1.28.0 \
    NJS_VERSION=0.8.11 \
    NJS_RELEASE=3~bookworm \
    PKG_RELEASE=1~bookworm

RUN set -x && \
    rm -rf /var/lib/apt/lists/*

COPY docker-entrypoint.sh /
COPY docker-entrypoint.d/ /docker-entrypoint.d/

RUN chmod +x /docker-entrypoint.sh && \
    chmod +x /docker-entrypoint.d/*.sh || true

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]

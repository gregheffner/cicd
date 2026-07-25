# Node backend proxy for the radar weathermap.
#
# Multi-stage on purpose. The whole reason radar accumulated 135 CVEs was that
# nothing in the final image was ever refreshed, and the single biggest source
# of findings was never this app's code at all:
#   - stale libcrypto3/libssl3 in the base   -> fixed by `apk upgrade` below
#   - npm's OWN bundled deps (tar, minimatch, glob, brace-expansion, ...)
#     -> fixed by NOT SHIPPING NPM. The runtime entrypoint is `node server.js`,
#        so npm is a build-time tool only. It stays in the builder stage.
#
# Base is pinned to a patch tag so bumps are deliberate. To bump: check
# https://hub.docker.com/_/node/tags?name=20 for the newest 20.x-alpine, update
# BOTH FROM lines below, and let the pipeline's Trivy gate confirm it's clean.

# ---- builder: installs deps, never ships ----
FROM node:20.19-alpine AS builder

WORKDIR /app

# package-lock.json is committed, so `npm ci` gives reproducible installs.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# ---- runtime ----
FROM node:20.19-alpine

# Patched libcrypto3/libssl3/musl/zlib from the Alpine repos, then drop npm and
# its bundled dependency tree entirely — it is not needed to run the app.
RUN apk upgrade --no-cache \
    && rm -rf /usr/local/lib/node_modules/npm \
              /usr/local/bin/npm \
              /usr/local/bin/npx

WORKDIR /app

COPY --from=builder --chown=node:node /app/node_modules ./node_modules

# Copy app files. .dockerignore excludes node_modules, .env and every key/cert
# pattern, so no secret from the working tree can land in the image.
COPY --chown=node:node . .

# server.js readFileSync's server.key/server.cert at startup and crashes without
# them, but the repo copy must never ship in a PUBLIC image. Generate a fresh
# throwaway pair at build time instead: the HTTPS:5000 listener is dev-only
# (Kubernetes uses HTTP:8080 and Cloudflare terminates real TLS at the edge),
# so this cert is never on a trusted path. openssl is removed again afterwards.
RUN apk add --no-cache --virtual .gen-certs openssl \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
       -subj "/CN=radar.local" \
       -keyout /app/server.key -out /app/server.cert \
    && chown node:node /app/server.key /app/server.cert \
    && chmod 600 /app/server.key \
    && apk del .gen-certs

# Run as the unprivileged 'node' user; both ports are >1024 so no root needed
USER node

# HTTPS listener (local/dev) on 5000; HTTP listener (Kubernetes) on 8080
EXPOSE 5000
EXPOSE 8080

ENV NODE_ENV=production

# Exec-form node directly: package.json has no start script, and skipping the
# npm wrapper gives the process correct signal handling (PID 1 gets SIGTERM).
CMD ["node", "server.js"]

# Node backend proxy for the radar weathermap.
# Base pinned to a patch tag so builds are reproducible-ish and CVE fixes are
# deliberate. To bump: check https://hub.docker.com/_/node/tags?name=20 for the
# newest 20.x-alpine, update the line below, and let the pipeline's Trivy gate
# confirm it's clean.
FROM node:20.19-alpine

# The base image ships a stale OpenSSL (libcrypto3/libssl3) and a stale bundled
# npm; between them they accounted for ~93 of the 135 CVEs in the old image.
# Neither comes from this app's code, and both are fixed by upgrading in place:
#   - apk upgrade  -> patched libcrypto3/libssl3 + musl/zlib from Alpine repos
#   - npm@latest   -> patched tar/minimatch/glob/brace-expansion/cross-spawn
#                     (these are npm's OWN bundled deps, not ours)
# Re-run the weekly pipeline to keep both current.
# npm is pinned to the 11.x line: npm@latest is 12.x, which requires Node >=22
# and fails EBADENGINE on this base. Bump when the base moves off Node 20.
RUN apk upgrade --no-cache && npm install -g npm@11 && npm cache clean --force

WORKDIR /app

# package-lock.json is committed, so use `npm ci` for reproducible installs.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

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

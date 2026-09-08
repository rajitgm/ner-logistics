# Web image (Next.js). Referenced by the "web" compose profile.
#
# apps/web does not exist yet — Phase 2 creates it. This file is here so the
# compose profile has something to point at and so the build strategy is settled
# before there is code to argue about; `docker compose --profile web build` will
# fail with "apps/web/package.json not found" until then, which is accurate.
#
# Build from the repository root:
#   docker build -f infrastructure/docker/web.Dockerfile -t ner-web .

# ------------------------------------------------------------------------- deps
FROM node:22-alpine AS deps
WORKDIR /app
# Lockfile only, so the install layer survives every source edit.
COPY apps/web/package.json apps/web/package-lock.json* ./
# npm ci needs a lockfile; falling back to install keeps a fresh scaffold
# buildable before the lockfile is committed.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# ---------------------------------------------------------------------- builder
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web ./
# NEXT_PUBLIC_* values are inlined at build time, so they must be present here
# and not only at runtime.
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
ARG NEXT_PUBLIC_WS_BASE_URL=ws://localhost:8000/api/v1/ws
ARG NEXT_PUBLIC_MAP_STYLE_URL=https://demotiles.maplibre.org/style.json
ARG NEXT_PUBLIC_DEFAULT_LOCALE=en
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_WS_BASE_URL=$NEXT_PUBLIC_WS_BASE_URL \
    NEXT_PUBLIC_MAP_STYLE_URL=$NEXT_PUBLIC_MAP_STYLE_URL \
    NEXT_PUBLIC_DEFAULT_LOCALE=$NEXT_PUBLIC_DEFAULT_LOCALE \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------------------------------------------------------------------- runtime
FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN addgroup -g 10002 nodegrp && adduser -u 10002 -G nodegrp -S nextjs
# Requires `output: "standalone"` in next.config: it emits a minimal server plus
# only the node_modules actually reached, which is a fraction of the dev tree.
COPY --from=builder --chown=nextjs:nodegrp /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodegrp /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodegrp /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]

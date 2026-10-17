# syntax=docker/dockerfile:1
#
# Builds the storefront and serves it with nginx, which also reverse-proxies
# the services on the same origin (see frontend/nginx.conf).
#
#   docker build -f frontend/Dockerfile -t microshop-frontend .
#
# The build context is the repository root.
FROM node:26-alpine AS build

WORKDIR /frontend

# Dependencies first so the layer is cached across source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json frontend/vite.config.ts frontend/vitest.config.ts frontend/index.html ./
COPY frontend/src ./src
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginx:1.29-alpine AS runtime

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html

EXPOSE 80

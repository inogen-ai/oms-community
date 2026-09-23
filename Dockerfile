# This build context is the allowlisted public export, never the private tree.
FROM ghcr.io/astral-sh/uv:0.10.4 AS uv
FROM python:3.12-slim-bookworm AS api-build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS api
RUN apt-get update && apt-get install --yes --no-install-recommends git openssh-client acl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home oms && mkdir /data /published && chown oms:oms /data /published
COPY --from=api-build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 4317
CMD ["oms", "serve", "--host", "0.0.0.0", "--port", "4317"]

FROM node:22-bookworm-slim AS ui-build
WORKDIR /build/frontend
COPY frontend/packages ./packages
# Keep local-package installation consistent with the checked-in lockfile.
COPY frontend/community/package.json frontend/community/package-lock.json frontend/community/.npmrc ./community/
WORKDIR /build/frontend/community
RUN npm ci
COPY frontend/community ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM nginx:1.28-alpine AS ui
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=ui-build /build/frontend/community/out /usr/share/nginx/html
EXPOSE 80

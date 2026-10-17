# syntax=docker/dockerfile:1
#
# One parameterised image for every service:
#
#   docker build -f docker/Dockerfile --build-arg SERVICE=order-service -t order-service .
#
# The build installs the shared runtime library plus the service package, then
# copies the service migrations next to the app (they run at startup).
FROM python:3.12-slim AS builder

ARG SERVICE
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY libs/common /build/libs/common
COPY services/${SERVICE} /build/services/${SERVICE}

# Install common first so its local version satisfies the service dependency.
RUN pip install /build/libs/common \
 && pip install /build/services/${SERVICE}

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG SERVICE
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    MIGRATIONS_DIR=/app/migrations

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --from=builder /build/services/${SERVICE}/migrations /app/migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && useradd --create-home --uid 10001 app \
 && chown -R app:app /app

USER app
EXPOSE 8000

# SERVICE_MODULE (e.g. order_service.main:app) and PORT come from compose.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

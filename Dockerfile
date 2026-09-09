FROM python:3.12-slim

ARG ATTENTION_ROUTER_BUILD_SHA=unknown
ARG GIT_SHA=$ATTENTION_ROUTER_BUILD_SHA
LABEL org.opencontainers.image.revision=$ATTENTION_ROUTER_BUILD_SHA \
      org.opencontainers.image.source=https://github.com/escossio/attention-router
ARG ATTENTION_ROUTER_IMAGE_VERSION=dev
LABEL org.opencontainers.image.version=$ATTENTION_ROUTER_IMAGE_VERSION \
      org.opencontainers.image.title="Attention Router" \
      org.opencontainers.image.description="Policy-controlled conversational routing for contextual personal agents" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV ATTENTION_ROUTER_BUILD_SHA=$ATTENTION_ROUTER_BUILD_SHA \
    RUNTIME_HEAD=$ATTENTION_ROUTER_BUILD_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ffmpeg && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /app/
COPY attention_router /app/attention_router
COPY alembic /app/alembic
COPY tests /app/tests
COPY scripts /app/scripts
COPY config /app/config
COPY ops /app/ops
COPY alembic.ini /app/alembic.ini
RUN pip install --no-cache-dir -e ".[dev]"
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD sh -c 'curl -fsS http://127.0.0.1:${HTTP_PORT:-18100}/health/live || exit 1'

# Build only in authorized CI. Both base digest and source SHA are mandatory.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG SOURCE_SHA
RUN test "${#SOURCE_SHA}" = 40 && case "$SOURCE_SHA" in *[!0-9a-f]*) exit 1;; esac
LABEL org.opencontainers.image.revision=$SOURCE_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY attention_router /app/attention_router
COPY experiments/incremental_memory /app/experiments/incremental_memory
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes --report=/app/m2-install.json -r /app/experiments/incremental_memory/device/requirements.lock
USER 10001:10001
CMD ["uvicorn", "experiments.incremental_memory.device.runtime:load_app", "--factory", "--uds", "/http/app.sock", "--no-access-log"]

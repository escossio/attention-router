FROM python:3.13-slim
ARG ATTENTION_ROUTER_BUILD_SHA=unverified
LABEL org.opencontainers.image.source="https://github.com/escossio/attention-router" \
      org.opencontainers.image.revision="${ATTENTION_ROUTER_BUILD_SHA}"
RUN groupadd --gid 10001 relay && useradd --uid 10001 --gid 10001 --no-create-home relay
WORKDIR /app
COPY ops/occurrence-relay/relay.py /app/relay.py
RUN chmod 0444 /app/relay.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "-I", "/app/relay.py"]

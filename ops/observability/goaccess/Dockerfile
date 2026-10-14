# ROC GoAccess preserves the Debian 13 native GoAccess 1.9.x capabilities.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends goaccess ca-certificates coreutils gawk && rm -rf /var/lib/apt/lists/*
COPY start-goaccess.sh /usr/local/bin/start-goaccess.sh
RUN chmod 0555 /usr/local/bin/start-goaccess.sh
USER 10001:4
ENTRYPOINT ["/usr/local/bin/start-goaccess.sh"]

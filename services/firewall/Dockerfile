FROM mitmproxy/mitmproxy:11

USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl jq && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /certs && chown mitmproxy:mitmproxy /certs
USER mitmproxy

COPY addon.py /app/addon.py
COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 8080 8081

ENTRYPOINT ["/entrypoint.sh"]

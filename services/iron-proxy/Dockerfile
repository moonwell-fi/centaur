FROM ironsh/iron-proxy:0.26.0@sha256:6b0a34c58bf4b9fa0d3bd7f046f0c9b32feb8116011eed11d98c91c5642b7fa5

USER root
RUN apk add --no-cache curl jq

COPY services/iron-proxy/iron-proxy.yaml /usr/local/share/iron-proxy/proxy.yaml.default
COPY --chmod=755 services/iron-proxy/entrypoint.sh /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]

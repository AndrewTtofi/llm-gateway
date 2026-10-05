# Toxiproxy from the official release binary, checksum-verified (the ghcr image wasn't
# pullable from this environment). Bench rig only.
FROM alpine:3.24
ARG VERSION=v2.12.0
ARG SHA256=556d891134a3c582dc1e1a3f7335fd55142e5965769855a00b944e13e48302fc
ADD https://github.com/Shopify/toxiproxy/releases/download/${VERSION}/toxiproxy-server-linux-amd64 /usr/local/bin/toxiproxy
RUN echo "${SHA256}  /usr/local/bin/toxiproxy" | sha256sum -c - && chmod +x /usr/local/bin/toxiproxy
USER nobody
ENTRYPOINT ["/usr/local/bin/toxiproxy"]

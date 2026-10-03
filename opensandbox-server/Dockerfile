# syntax=docker/dockerfile:1.7

FROM opensandbox/server:v0.2.3@sha256:ae8dfbb277f40a39ff01ef35e5e1c10675acfe0fa9db15259b8f323e5efab778

ARG SERVER_VERSION=0.2.3-tinkerfin.1
ARG VCS_REF=unknown

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade \
        gzip=1.13-1+deb13u1 \
        libpcre2-8-0=10.46-1~deb13u3 \
        libsqlite3-0=3.46.1-7+deb13u2 \
        libssl3t64=3.5.7-1~deb13u3 \
        openssl=3.5.7-1~deb13u3 \
        openssl-provider-legacy=3.5.7-1~deb13u3 \
        perl-base=5.40.1-6+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,source=opensandbox-server/security-requirements.txt,target=/tmp/security-requirements.txt \
    --mount=type=bind,source=opensandbox-server/build-requirements.txt,target=/tmp/build-requirements.txt \
    /usr/local/bin/python -m pip install --no-cache-dir --require-hashes --no-deps \
        --only-binary=:all: --requirement /tmp/build-requirements.txt \
    && /usr/local/bin/python -m pip --python /app/.venv install \
        --no-cache-dir --require-hashes --no-deps --only-binary=:all: \
        --requirement /tmp/security-requirements.txt \
    && /usr/local/bin/python -m pip check \
    && /usr/local/bin/python -m pip --python /app/.venv check

RUN --mount=type=bind,source=opensandbox-server/apply-patch.py,target=/tmp/apply-patch.py \
    python /tmp/apply-patch.py

LABEL org.opencontainers.image.title="TinkerFin OpenSandbox Server" \
      org.opencontainers.image.description="OpenSandbox Server with private-network session provisioning" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.documentation="https://github.com/tinkerfin-ai/sandbox-runtime/tree/main/opensandbox-server" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${SERVER_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.base.name="opensandbox/server:v0.2.3" \
      org.opencontainers.image.base.digest="sha256:ae8dfbb277f40a39ff01ef35e5e1c10675acfe0fa9db15259b8f323e5efab778"

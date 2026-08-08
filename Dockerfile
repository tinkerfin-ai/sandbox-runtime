# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.11.15-slim-trixie

FROM ${PYTHON_IMAGE} AS downloads

ARG TARGETARCH
ARG NODE_VERSION=22.23.2
ARG NPM_VERSION=10.9.9
ARG GO_VERSION=1.25.7
ARG NODE_SHA256_AMD64=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG NPM_SHA512=d60fba8cb42f688b81e33c2f1cbef2ad7b977166700ec0ad057f1b6d60ea6ef2524abf673e20c35931cd8305d1dbb8887134d6eefdc0e7b8435bd458bf65b862
ARG GO_SHA256_AMD64=12e6d6a191091ae27dc31f6efc630e3a3b8ba409baf3573d955b196fdf086005
ARG GO_SHA256_ARM64=ba611a53534135a81067240eff9508cd7e256c560edd5d8c2fef54f083c07129

SHELL ["/bin/bash", "-Eeuo", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH}" in \
        amd64) node_arch=x64; node_sha256="${NODE_SHA256_AMD64}"; go_sha256="${GO_SHA256_AMD64}" ;; \
        arm64) node_arch=arm64; node_sha256="${NODE_SHA256_ARM64}"; go_sha256="${GO_SHA256_ARM64}" ;; \
        *) printf 'unsupported TARGETARCH: %s\n' "${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
    && npm_archive="npm-${NPM_VERSION}.tgz" \
    && go_archive="go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
    && curl --fail --location --retry 5 --output "/tmp/${node_archive}" \
        "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}" \
    && printf '%s  %s\n' "${node_sha256}" "/tmp/${node_archive}" | sha256sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${npm_archive}" \
        "https://registry.npmjs.org/npm/-/${npm_archive}" \
    && printf '%s  %s\n' "${NPM_SHA512}" "/tmp/${npm_archive}" | sha512sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${go_archive}" \
        "https://go.dev/dl/${go_archive}" \
    && printf '%s  %s\n' "${go_sha256}" "/tmp/${go_archive}" | sha256sum --check --strict \
    && install -d /opt/sandbox-runtime/node /opt/sandbox-runtime/go \
    && tar --extract --xz --file "/tmp/${node_archive}" \
        --directory /opt/sandbox-runtime/node --strip-components 1 \
    && PATH=/opt/sandbox-runtime/node/bin:${PATH} \
        npm install --global --offline "/tmp/${npm_archive}" \
    && tar --extract --gzip --file "/tmp/${go_archive}" \
        --directory /opt/sandbox-runtime/go --strip-components 1

FROM ${PYTHON_IMAGE}

ARG RUNTIME_VERSION=0.1.0
ARG PYTHON_VERSION=3.11.15
ARG JAVA_VERSION=21
ARG NODE_VERSION=22.23.2
ARG NPM_VERSION=10.9.9
ARG GO_VERSION=1.25.7
ARG MAVEN_VERSION=3.9.9
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="TinkerFin Sandbox Runtime" \
      org.opencontainers.image.description="Multi-architecture OpenSandbox runtime for Python, Java, Node.js, Go, and Maven" \
      org.opencontainers.image.url="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.documentation="https://github.com/tinkerfin-ai/sandbox-runtime#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${RUNTIME_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

SHELL ["/bin/bash", "-Eeuo", "pipefail", "-c"]

ENV VIRTUAL_ENV=/opt/sandbox-runtime/venv \
    JAVA_HOME=/opt/sandbox-runtime/jdk \
    NODE_HOME=/opt/sandbox-runtime/node \
    GOROOT=/opt/sandbox-runtime/go \
    MAVEN_HOME=/opt/sandbox-runtime/maven \
    MPLBACKEND=Agg \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_INDEX_URL=https://pypi.org/simple \
    NPM_CONFIG_REGISTRY=https://registry.npmjs.org/ \
    GOPROXY=https://proxy.golang.org,direct \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENV PATH=/opt/sandbox-runtime/venv/bin:/opt/sandbox-runtime/node/bin:/opt/sandbox-runtime/go/bin:/opt/sandbox-runtime/jdk/bin:/opt/sandbox-runtime/maven/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

COPY apt.conf /etc/apt/apt.conf.d/80-sandbox-runtime

RUN rm -f /etc/apt/apt.conf.d/docker-clean

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        file \
        git \
        jq \
        openssh-client \
        pkg-config \
        procps \
        ripgrep \
        unzip \
        wget \
        zip

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install --yes --no-install-recommends build-essential

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-21-jdk-headless

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install --yes --no-install-recommends maven

RUN install -d /opt/sandbox-runtime /workspace \
    && java_home=$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")") \
    && ln -s "${java_home}" /opt/sandbox-runtime/jdk \
    && ln -s /usr/local /opt/sandbox-runtime/python \
    && ln -s /usr/share/maven /opt/sandbox-runtime/maven \
    && [[ $(javac -version 2>&1) == "javac ${JAVA_VERSION}"* ]] \
    && [[ $(mvn --version) == *"Apache Maven ${MAVEN_VERSION}"* ]] \
    && [[ $(python --version) == "Python ${PYTHON_VERSION}" ]]

COPY --from=downloads /opt/sandbox-runtime/node /opt/sandbox-runtime/node
COPY --from=downloads /opt/sandbox-runtime/go /opt/sandbox-runtime/go
COPY pip.conf /etc/pip.conf
COPY requirements.in requirements.lock /opt/sandbox-runtime/

RUN python -m venv "${VIRTUAL_ENV}" \
    && "${VIRTUAL_ENV}/bin/pip" install \
        --require-hashes --only-binary=:all: \
        --requirement /opt/sandbox-runtime/requirements.lock \
    && "${VIRTUAL_ENV}/bin/pip" check \
    && find "${VIRTUAL_ENV}" -type d -name __pycache__ -prune -exec rm -rf '{}' + \
    && [[ $(node --version) == "v${NODE_VERSION}" ]] \
    && [[ $(npm --version) == "${NPM_VERSION}" ]] \
    && [[ $(go version) == *" go${GO_VERSION} "* ]]

COPY --chmod=0755 scripts/entrypoint.sh /opt/sandbox-runtime/bin/entrypoint.sh

WORKDIR /workspace
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/sandbox-runtime/bin/entrypoint.sh"]
CMD []

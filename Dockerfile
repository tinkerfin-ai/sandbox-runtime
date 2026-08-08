# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.11.15-slim-trixie

FROM ${PYTHON_IMAGE} AS downloads

ARG TARGETARCH
ARG NODE_VERSION=22.23.2
ARG NPM_VERSION=12.0.2
ARG GO_VERSION=1.25.12
ARG SETUPTOOLS_VERSION=84.0.0
ARG NPM_BRACE_EXPANSION_VERSION=5.0.9
ARG NPM_IP_ADDRESS_VERSION=10.3.1
ARG NODE_SHA256_AMD64=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG NPM_SHA512=b885e890b9418fa1693544d05f53e64f9a73ec194837d4258b15fecdd692347b1dd2a517b1b0cbaf9d31cd8e92c3b70956bd2ecc72833a57b4b3098f5bfa7943
ARG NPM_BRACE_EXPANSION_SHA512=49c43822ebc8105d533253fb66dfaf8c9ffff7394f6f64837315b13376e4f2ceade8619d27b28ed5d09c4e274e3c929e3d6df42c4ff6713ef00b23e1a3dfd6c6
ARG NPM_IP_ADDRESS_SHA512=d5ef5dde46fdecd1c94c8243656f6b2aa5b687af9d15ae740f2d1fa4f48c429d800e37b982f2ac5e67622ba770639b7be93693b79f8fe4dd58fcba13a08c4fea
ARG SETUPTOOLS_SHA256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670
ARG GO_SHA256_AMD64=234828b7a89e0e303d2556310ee549fbcf253d28de937bac3da13d6294262ac1
ARG GO_SHA256_ARM64=8b5884aef89600aef5b0b051fb971f11f49bb996521e911f30f02a66884f7bd2

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
    && brace_expansion_archive="brace-expansion-${NPM_BRACE_EXPANSION_VERSION}.tgz" \
    && ip_address_archive="ip-address-${NPM_IP_ADDRESS_VERSION}.tgz" \
    && setuptools_wheel="setuptools-${SETUPTOOLS_VERSION}-py3-none-any.whl" \
    && go_archive="go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
    && curl --fail --location --retry 5 --output "/tmp/${node_archive}" \
        "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}" \
    && printf '%s  %s\n' "${node_sha256}" "/tmp/${node_archive}" | sha256sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${npm_archive}" \
        "https://registry.npmjs.org/npm/-/${npm_archive}" \
    && printf '%s  %s\n' "${NPM_SHA512}" "/tmp/${npm_archive}" | sha512sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${brace_expansion_archive}" \
        "https://registry.npmjs.org/brace-expansion/-/${brace_expansion_archive}" \
    && printf '%s  %s\n' "${NPM_BRACE_EXPANSION_SHA512}" "/tmp/${brace_expansion_archive}" \
        | sha512sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${ip_address_archive}" \
        "https://registry.npmjs.org/ip-address/-/${ip_address_archive}" \
    && printf '%s  %s\n' "${NPM_IP_ADDRESS_SHA512}" "/tmp/${ip_address_archive}" \
        | sha512sum --check --strict \
    && curl --fail --location --retry 5 --output /tmp/setuptools.whl \
        "https://files.pythonhosted.org/packages/py3/s/setuptools/${setuptools_wheel}" \
    && printf '%s  %s\n' "${SETUPTOOLS_SHA256}" /tmp/setuptools.whl \
        | sha256sum --check --strict \
    && curl --fail --location --retry 5 --output "/tmp/${go_archive}" \
        "https://go.dev/dl/${go_archive}" \
    && printf '%s  %s\n' "${go_sha256}" "/tmp/${go_archive}" | sha256sum --check --strict \
    && install -d /opt/sandbox-runtime/node /opt/sandbox-runtime/go \
    && tar --extract --xz --file "/tmp/${node_archive}" \
        --directory /opt/sandbox-runtime/node --strip-components 1 \
    && PATH=/opt/sandbox-runtime/node/bin:${PATH} \
        npm install --global --offline "/tmp/${npm_archive}" \
    && npm_root=/opt/sandbox-runtime/node/lib/node_modules/npm \
    && rm -rf \
        "${npm_root}/node_modules/brace-expansion" \
        "${npm_root}/node_modules/ip-address" \
    && install -d \
        "${npm_root}/node_modules/brace-expansion" \
        "${npm_root}/node_modules/ip-address" \
    && tar --extract --gzip --file "/tmp/${brace_expansion_archive}" \
        --directory "${npm_root}/node_modules/brace-expansion" --strip-components 1 \
    && tar --extract --gzip --file "/tmp/${ip_address_archive}" \
        --directory "${npm_root}/node_modules/ip-address" --strip-components 1 \
    && [[ $(/opt/sandbox-runtime/node/bin/node -p \
            "require('${npm_root}/node_modules/brace-expansion/package.json').version") \
            == "${NPM_BRACE_EXPANSION_VERSION}" ]] \
    && [[ $(/opt/sandbox-runtime/node/bin/node -p \
            "require('${npm_root}/node_modules/ip-address/package.json').version") \
            == "${NPM_IP_ADDRESS_VERSION}" ]] \
    && tar --extract --gzip --file "/tmp/${go_archive}" \
        --directory /opt/sandbox-runtime/go --strip-components 1

FROM ${PYTHON_IMAGE}

ARG RUNTIME_VERSION=0.1.1
ARG PYTHON_VERSION=3.11.15
ARG JAVA_VERSION=21
ARG NODE_VERSION=22.23.2
ARG NPM_VERSION=12.0.2
ARG GO_VERSION=1.25.12
ARG MAVEN_VERSION=3.9.9
ARG SETUPTOOLS_VERSION=84.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG DEBIAN_FRONTEND=noninteractive

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

RUN --mount=type=bind,from=downloads,source=/tmp/setuptools.whl,target=/tmp/setuptools-${SETUPTOOLS_VERSION}-py3-none-any.whl \
    /usr/local/bin/python -m pip install --root-user-action=ignore --no-index --no-deps \
        "/tmp/setuptools-${SETUPTOOLS_VERSION}-py3-none-any.whl" \
    && /usr/local/bin/python -m venv "${VIRTUAL_ENV}" \
    && "${VIRTUAL_ENV}/bin/pip" install --no-index --no-deps \
        "/tmp/setuptools-${SETUPTOOLS_VERSION}-py3-none-any.whl" \
    && "${VIRTUAL_ENV}/bin/pip" install \
        --require-hashes --only-binary=:all: \
        --requirement /opt/sandbox-runtime/requirements.lock \
    && "${VIRTUAL_ENV}/bin/pip" check \
    && find "${VIRTUAL_ENV}" -type d -name __pycache__ -prune -exec rm -rf '{}' + \
    && [[ $(node --version) == "v${NODE_VERSION}" ]] \
    && [[ $(npm --version) == "${NPM_VERSION}" ]] \
    && [[ $(go version) == *" go${GO_VERSION} "* ]] \
    && [[ $(python -c 'import setuptools; print(setuptools.__version__)') \
        == "${SETUPTOOLS_VERSION}" ]] \
    && [[ $(/usr/local/bin/python -c 'import setuptools; print(setuptools.__version__)') \
        == "${SETUPTOOLS_VERSION}" ]]

COPY --chmod=0755 scripts/entrypoint.sh /opt/sandbox-runtime/bin/entrypoint.sh

WORKDIR /workspace
STOPSIGNAL SIGTERM
LABEL org.opencontainers.image.title="TinkerFin Sandbox Runtime" \
      org.opencontainers.image.description="Multi-architecture OpenSandbox runtime for Python, Java, Node.js, Go, and Maven" \
      org.opencontainers.image.url="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.documentation="https://github.com/tinkerfin-ai/sandbox-runtime#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${RUNTIME_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"
ENTRYPOINT ["/opt/sandbox-runtime/bin/entrypoint.sh"]
CMD []

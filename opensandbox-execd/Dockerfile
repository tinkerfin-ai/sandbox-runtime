# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM --platform=$BUILDPLATFORM golang:1.25.13@sha256:cbff9d1a9041b316010f2da6b701b6c0d597718cb90928c85eb597334a0d23d4 AS source

ENV GOTOOLCHAIN=local GOFLAGS=-mod=readonly

ADD --checksum=sha256:6dba6199299ea0252baac47cc6bb901e41389e811d505ed93949b97ad0061a90 \
    https://codeload.github.com/alibaba/OpenSandbox/legacy.tar.gz/4a9db411879601610843af9c8e03563694325b2a /tmp/upstream.tar.gz
RUN mkdir /source && tar -xzf /tmp/upstream.tar.gz -C /source --strip-components=1
COPY opensandbox-execd/patches/ /patches/
WORKDIR /source
RUN git apply --check /patches/isolation.patch /patches/control-auth.patch \
    && git apply /patches/isolation.patch /patches/control-auth.patch \
    && git apply --check /patches/control-root.patch \
    && git apply /patches/control-root.patch \
    && git apply --check /patches/probe-ownership.patch \
    && git apply /patches/probe-ownership.patch \
    && git apply --check /patches/lifecycle-tests.patch \
    && git apply /patches/lifecycle-tests.patch \
    && git apply --check /patches/capability-drop.patch \
    && git apply /patches/capability-drop.patch \
    && git apply --check /patches/security-dependencies.patch \
    && git apply /patches/security-dependencies.patch
WORKDIR /source/components/internal
RUN go mod download && go mod verify
WORKDIR /source/components/execd
RUN go mod download && go mod verify

FROM source AS test
RUN go test ./pkg/isolation ./pkg/runtime ./pkg/web/model ./pkg/web/controller ./pkg/web \
    -run 'Test(MergedView|DeclaredSession|Namespace|Background|ReadIsolatedRunExitCode|Proxy|Isolated|UpperManager|DeleteSession|FailedStartup|StartupTerminalError)' -count=1

FROM test AS builder
ARG TARGETOS
ARG TARGETARCH
ARG EXECD_VERSION=1.0.22-tinkerfin.1
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath -buildvcs=false \
    -ldflags "-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=${EXECD_VERSION} -X github.com/alibaba/opensandbox/internal/version.GitCommit=4a9db411879601610843af9c8e03563694325b2a" \
    -o /build/execd ./main.go
RUN CGO_ENABLED=0 GOOS=windows GOARCH=${TARGETARCH} go build -trimpath -buildvcs=false \
    -ldflags "-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=${EXECD_VERSION} -X github.com/alibaba/opensandbox/internal/version.GitCommit=4a9db411879601610843af9c8e03563694325b2a" \
    -o /build/execd.exe ./main.go
WORKDIR /source/components/internal
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath -buildvcs=false \
    -ldflags "-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=${EXECD_VERSION} -X github.com/alibaba/opensandbox/internal/version.GitCommit=4a9db411879601610843af9c8e03563694325b2a" \
    -o /build/opensandbox-supervisor ./cmd/supervisor

FROM scratch AS openssl-amd64
ADD --checksum=sha256:6632d758d8f5e9ea3b650fe966f23bbf9a202f8b8dceecac93da135dec5e3689 \
    https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libcrypto3-3.5.9-r0.apk /packages/libcrypto3.apk
ADD --checksum=sha256:05e3393fb95aa5751ca2f9d242f659f6cff82c1cc7767cc2df4a086f7ad01877 \
    https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libssl3-3.5.9-r0.apk /packages/libssl3.apk

FROM scratch AS openssl-arm64
ADD --checksum=sha256:2676a2b0b6e23ea2edccf3ee982b9842a665d52603d047de3d0a185dc316d983 \
    https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/libcrypto3-3.5.9-r0.apk /packages/libcrypto3.apk
ADD --checksum=sha256:20ac252b276d73f2c69c1d25f84537c7fba81caefc026c6be1094b394af2082e \
    https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/libssl3-3.5.9-r0.apk /packages/libssl3.apk

FROM openssl-${TARGETARCH} AS openssl

FROM opensandbox/execd:v1.0.22@sha256:0d8f44cf4194732719aa79999d4b120c98bdab02bc61e9ad13f75f83af4c2684
ARG EXECD_VERSION=1.0.22-tinkerfin.1
ARG VCS_REF=unknown
RUN --mount=type=bind,from=openssl,source=/packages,target=/packages \
    apk add --no-cache --no-network /packages/libcrypto3.apk /packages/libssl3.apk
COPY --from=builder /build/execd /execd
COPY --from=builder /build/execd.exe /execd.exe
COPY --from=builder /build/opensandbox-supervisor /opensandbox-supervisor
LABEL org.opencontainers.image.title="TinkerFin OpenSandbox Execd" \
      org.opencontainers.image.description="OpenSandbox execd with owned isolated sessions and confined file operations" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/sandbox-runtime" \
      org.opencontainers.image.documentation="https://github.com/tinkerfin-ai/sandbox-runtime/tree/main/opensandbox-execd" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${EXECD_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.base.name="opensandbox/execd:v1.0.22" \
      org.opencontainers.image.base.digest="sha256:0d8f44cf4194732719aa79999d4b120c98bdab02bc61e9ad13f75f83af4c2684"

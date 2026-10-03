# OpenSandbox Server

This image runs OpenSandbox Server 0.2.3 and provisions native execd sessions
with a private network namespace. It is separate from the
[`sandbox-runtime`](../README.md) image used to execute sandbox commands.

## Build and verify

From the repository root:

```bash
make server-build SERVER_IMAGE=opensandbox-server:dev
make server-test SERVER_IMAGE=opensandbox-server:dev
```

Docker Buildx selects `linux/amd64` or `linux/arm64` for the current host.
The build uses the upstream OCI index
`sha256:ae8dfbb277f40a39ff01ef35e5e1c10675acfe0fa9db15259b8f323e5efab778`
and verifies the source against
[OpenSandbox commit c39b814](https://github.com/alibaba/OpenSandbox/blob/c39b814f36ded4c61d5ac6f9332ee4dfbab86c00/server/opensandbox_server/services/docker/docker_service.py).
The correction is applied during the build; an unexpected upstream source
causes the build to fail.

The image test checks the installed server's Docker provisioning requests with
isolation absent, disabled and enabled, both with and without an egress policy.
It checks capabilities, security options, resource limits, network routing and
parent authentication, including token removal when proxying to user ports.
It runs without a Docker socket inside the test container and removes only its
own labeled container. It does not certify application workspace isolation.
Signal checks interrupt the owning shell with `SIGINT` and `SIGTERM` after the
container reports readiness and verify cleanup and the corresponding exit code.

## Deploy

Use this image for the OpenSandbox control plane. Keep the existing server
configuration, command and Docker access described in the
[upstream deployment guide](https://github.com/alibaba/OpenSandbox/tree/c39b814f36ded4c61d5ac6f9332ee4dfbab86c00/server).
Use the controlled [OpenSandbox Execd](../opensandbox-execd/README.md) image
`ghcr.io/tinkerfin-ai/opensandbox-execd:1.0.22-tinkerfin.1` for
`runtime.execd_image`. Pin both server and execd OCI index digests in production.
Set `network_mode = "bridge"` in the server's `[docker]` section for isolated sessions. `none`
and user-defined networks with their own network namespace are also accepted.
The upstream default is `host`; isolated requests reject that default, explicit
host networking, host-network IDs and `container:<other>` before allocating
sandbox resources. A server-created egress sidecar remains supported with
`network_mode = "bridge"`. The server entrypoint and configuration schema are
unchanged.

The existing `bootstrap.execd.isolation=enable` extension grants `SYS_ADMIN`
and `NET_ADMIN` to the trusted sandbox parent, disables its AppArmor and seccomp
profiles, and mounts the session upper directory as tmpfs. `NET_ADMIN` allows
bubblewrap to initialize loopback in a private network namespace.
The extension does not make the container privileged. Requests that omit or
disable the extension keep the upstream capability and security defaults.

The extension also authenticates each sandbox's privileged parent. The server
and OpenSandbox SDK handle endpoint authentication, including after reconnect;
application code does not construct parent authentication headers. The server
and execd proxies remove parent credentials before forwarding to user ports.
Protect Docker access and container metadata, and do not pass parent credentials
into isolated-session environments.

Only trusted application code may access the control plane and raw execd APIs.
Untrusted commands must run as a non-root isolated-session user with an
explicitly restricted filesystem and network policy. The server image alone
does not provide project isolation. With an egress sidecar, the trusted parent
also holds `NET_ADMIN` in the shared parent network namespace; the sidecar is
not a security boundary against that parent.

## Publish

The `OpenSandbox Server` workflow verifies each architecture on a native runner.
A Git tag `server-v0.2.3-tinkerfin.1` matching `versions.env` publishes
`ghcr.io/tinkerfin-ai/opensandbox-server:0.2.3-tinkerfin.1` after its image tests
and vulnerability checks pass. Exact image tags cannot be overwritten.
Pin the resulting OCI index digest in deployment configuration.
Published registry artifacts are retained. Local image references pulled only
for verification are removed; images already present on the runner are borrowed.

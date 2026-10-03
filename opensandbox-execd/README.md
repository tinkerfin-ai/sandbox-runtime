# OpenSandbox Execd

This image provides isolated sessions with caller-owned identifiers, confirmed
PID namespace shutdown, and filesystem operations confined to the session's
opened workspace roots. It builds OpenSandbox execd 1.0.22 at commit
`4a9db411879601610843af9c8e03563694325b2a` with Go 1.25.13 and retains the pinned
upstream bubblewrap 0.11.2 and native session gate.

## Build and verify

```bash
docker pull ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.2
make execd-build EXECD_IMAGE=opensandbox-execd:dev
make execd-test EXECD_IMAGE=opensandbox-execd:dev
```

Docker Buildx selects the host's native `linux/amd64` or `linux/arm64` platform.
The integration test creates containers with unique ownership labels, validates
session cancellation, detached-process shutdown, rooted file operations and
FIFO-safe cleanup, and removes only its own containers. The signal checks cover
`SIGINT` and `SIGTERM`. It uses the runtime image from this repository; set
`EXECD_RUNTIME_IMAGE=sandbox-runtime:dev` to test with a local runtime build.
Test images must already be present locally. Private-network
sessions require the [OpenSandbox Server](../opensandbox-server/README.md)
provisioning configuration.

To run the Linux lifecycle, filesystem, background-run, and tagged bubblewrap
suites from the patched upstream checkout, use
`bash opensandbox-execd/tests/linux.sh /path/to/OpenSandbox opensandbox-execd:dev`.
These tests use Go 1.25.13 and the selected runtime image's Docker architecture.
`EXECD_RUNTIME_IMAGE` also selects the runtime for this script.

## Deploy

Set the controlled server's `runtime.execd_image` to
`ghcr.io/tinkerfin-ai/opensandbox-execd:1.0.22-tinkerfin.1` and pin its published
OCI index digest in production. Follow the
[server deployment requirements](../opensandbox-server/README.md#deploy) for
private networking and parent authentication. Use the authenticated endpoint
returned by the server; the OpenSandbox SDK applies its headers automatically.
The proxy forwards the parent access token only
to execd itself and removes it before forwarding to other user ports.

## Session ownership

Read `GET /v1/isolated/capabilities` and require
`available`, `client_session_ownership`, `namespace_exit_confirmation`, and
`rooted_filesystem` to be true. Keep the returned `session_namespace` with each
session identifier. It identifies the running daemon instance.

Caller-owned identifiers use the HTTP fields described here. This image does
not include an SDK distribution; upstream SDK create helpers do not send these
ownership fields.

Declare a canonical UUID `session_id` together with `session_namespace` in
`POST /v1/isolated/session`. The caller can query or cancel that identifier even
if the create response is lost. GET and LIST include sessions that are starting
or awaiting failed cleanup; LIST also identifies the workspace. Status is one
of `starting`, `active`, `stopping`, `cleanup_failed`, or `dead`.

Cancel with
`DELETE /v1/isolated/session/{session_id}?session_namespace={session_namespace}`.
A successful response confirms namespace termination and release of admitted
file operations and owned roots. Deleting an unknown valid identifier records
cancellation, so an outstanding create cannot later admit it. Repeated deletion
is successful. Creation with an identifier already used in this daemon returns
409. A mismatched daemon instance also returns 409.

For example, after reading the daemon's `session_namespace`:

```json
{
  "session_id": "a53107fa-6a75-41bd-9c56-2c714408ab1c",
  "session_namespace": "eb1443ac-ad93-47e2-8f21-bcb6bb7cbb86",
  "workspace": {"path": "/workspace/project-a", "mode": "rw"},
  "profile": "strict",
  "share_net": false,
  "uid_mode": "setpriv",
  "uid": 1000,
  "gid": 1000
}
```

The trusted host chooses workspace paths, bind mounts, user IDs, and network
policy. The workspace must permit the chosen UID to write. Session root mounts
and network restrictions still require explicit configuration; this image alone
does not hide other projects from shell commands.

The daemon retains at most 65,536 identifiers, including cancelled identifiers.
It never evicts cancellation records. At capacity, new identifiers receive 503;
cleanup of already recorded identifiers remains available. Session records
remain queryable after failed cleanup, and DELETE can be retried.

Background commands store their logs and exit status in `.execd/background-runs`
when writable, otherwise in the workspace root. Session deletion removes only
the session's recorded command files and pending check, not other project files.

A changed daemon instance does not confirm that a previous session has stopped.
Keep unresolved ownership and reject destructive workspace operations until
termination is independently established. Do not treat a missing session,
closed stream, or the outer bubblewrap monitor's exit as that confirmation.

## Publish

The `OpenSandbox Execd` workflow builds and tests on native amd64 and arm64
runners. A Git tag `execd-v1.0.22-tinkerfin.1` matching `versions.env` publishes
`ghcr.io/tinkerfin-ai/opensandbox-execd:1.0.22-tinkerfin.1` after the exact platform
digests pass native image tests and vulnerability checks. The workflow signs the
OCI index and publishes build provenance and SBOM attestations. Exact version
tags cannot be overwritten. Published registry artifacts are retained; locally
pulled verification images are removed only when that run owns them.

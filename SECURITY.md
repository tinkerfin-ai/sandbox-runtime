# Security policy

## Supported versions

Security fixes are released for the latest stable version. Older image digests
remain available but do not receive rebuilt operating-system or toolchain
updates.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/tinkerfin-ai/sandbox-runtime/security/advisories/new).
Include the affected image digest, architecture, reproduction steps, and impact.
Do not publish unpatched vulnerabilities or include credentials or user data.

This image supplies a user-space runtime. Container isolation, network policy,
resource limits, and secret management remain the responsibility of the sandbox
platform and its operator.

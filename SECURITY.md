# Security policy

## Supported versions

Security fixes are released for the latest stable version. Older image digests
remain available but do not receive rebuilt operating-system or toolchain
updates.

CI reports all high and critical findings and blocks releases when an upstream
fixed version is available. Findings without an upstream fix remain visible in
the scan output and are addressed in a new image release when a fix becomes
available.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/tinkerfin-ai/sandbox-runtime/security/advisories/new).
Include the affected image digest, architecture, reproduction steps, and impact.
Do not publish unpatched vulnerabilities or include credentials or user data.

This image supplies a user-space runtime. Container isolation, network policy,
resource limits, and secret management remain the responsibility of the sandbox
platform and its operator.

# Security policy

## Supported versions

Security updates are provided for the latest stable minor release. Older images
remain immutable for reproducibility but may no longer receive rebuilt base-image
or toolchain patches.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting form:

https://github.com/tinkerfin-ai/sandbox-runtime/security/advisories/new

Include the affected image digest, architecture, reproduction steps, and expected
impact. Do not open a public issue for an unpatched vulnerability and do not
include credentials or customer data.

A maintainer will acknowledge a complete report within five business days and
coordinate disclosure after a fixed image is available. Runtime isolation is
provided by the configured container or sandbox platform; this image does not
replace host-level isolation, network policy, resource limits, or secret
management.

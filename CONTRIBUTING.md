# Contributing

Contributions should keep the image reusable across OpenSandbox consumers and
both published architectures. Business files, credentials, private package
mirrors, and application-specific caches are out of scope.

## Development workflow

1. Update a version and its checksums in `versions.env`.
2. Keep the matching Dockerfile defaults synchronized so direct Buildx builds
   remain predictable.
3. When Python requirements change, run `make lock` and review every resolved
   package.
4. Run `make verify`, ShellCheck, and `make smoke` before opening a pull request.
5. Describe image-size and startup-time changes in the pull request.

Changes must not introduce a second version of a language runtime. A new default
package belongs in the image only when it is broadly useful, versionable, and
covered by the runtime smoke test.

## Releases

Maintainers publish releases from signed semantic-version tags. Pre-releases use
tags such as `v0.1.0-rc.1`; stable releases use tags such as `v0.1.0`. The release
workflow builds one OCI index, attaches SBOM and provenance attestations, scans
the resulting digest, and signs it with GitHub Actions keyless identity.

Published version tags are immutable. A correction requires a new version.

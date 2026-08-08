# Contributing

Issues and pull requests are welcome. Changes must support both `linux/amd64`
and `linux/arm64` and keep the image free of application files, credentials,
private registries, and business-specific dependencies.

## Pull requests

1. Update toolchain versions and archive checksums in `versions.env`.
2. If Python dependencies change, run `make lock` and review the resolved lock
   file.
3. Add or update a runtime smoke test for behavior changes.
4. Run the checks below and include relevant size or compatibility changes in
   the pull request.

```bash
make verify
make smoke IMAGE=sandbox-runtime:dev
```

Published version tags are immutable. Fixes are released under a new semantic
version.

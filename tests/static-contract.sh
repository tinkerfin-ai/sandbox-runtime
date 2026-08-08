#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

fail() {
    printf 'static contract failed: %s\n' "$*" >&2
    exit 1
}

assert_file() {
    local relative_path=$1
    [[ -f "${REPO_ROOT}/${relative_path}" ]] || fail "missing ${relative_path}"
}

assert_contains() {
    local relative_path=$1
    local pattern=$2
    rg --quiet -- "$pattern" "${REPO_ROOT}/${relative_path}" \
        || fail "${relative_path} does not contain ${pattern}"
}

assert_not_contains() {
    local pattern=$1
    if rg --hidden --glob '!.git/**' --glob '!tests/**' \
        --quiet -- "$pattern" "${REPO_ROOT}"; then
        fail "repository contains forbidden pattern ${pattern}"
    fi
}

required_files=(
    Dockerfile
    apt.conf
    versions.env
    requirements.in
    requirements.lock
    pip.conf
    scripts/entrypoint.sh
    tests/runtime-smoke.sh
    .github/workflows/ci.yml
    .github/workflows/release.yml
    LICENSE
    SECURITY.md
    THIRD_PARTY_NOTICES.md
)

for relative_path in "${required_files[@]}"; do
    assert_file "${relative_path}"
done

# shellcheck disable=SC1091
source "${REPO_ROOT}/versions.env"
[[ ${PYTHON_VERSION} =~ ^3\.11\.[0-9]+$ ]] || fail "Python must stay on 3.11"
[[ ${JAVA_VERSION} =~ ^21([.+_][0-9]+)*$ ]] || fail "Java must stay on 21"
[[ ${NODE_VERSION} =~ ^22\.[0-9]+\.[0-9]+$ ]] || fail "Node must stay on 22"
[[ ${GO_VERSION} =~ ^1\.25\.[0-9]+$ ]] || fail "Go must stay on 1.25"
[[ ${MAVEN_VERSION} =~ ^3\.9\.[0-9]+$ ]] || fail "Maven must stay on 3.9"

assert_contains Dockerfile 'VIRTUAL_ENV=/opt/sandbox-runtime/venv'
assert_contains Dockerfile 'JAVA_HOME=/opt/sandbox-runtime/jdk'
assert_contains Dockerfile 'GOROOT=/opt/sandbox-runtime/go'
assert_contains Dockerfile 'MAVEN_HOME=/opt/sandbox-runtime/maven'
assert_contains Dockerfile 'ENTRYPOINT \["/opt/sandbox-runtime/bin/entrypoint.sh"\]'
assert_contains apt.conf 'Acquire::Retries "5";'
assert_contains .github/workflows/ci.yml 'linux/amd64,linux/arm64'
assert_contains .github/workflows/release.yml 'ghcr.io/tinkerfin-ai/sandbox-runtime'

for package in numpy pandas matplotlib requests beautifulsoup4; do
    assert_contains requirements.in "^${package}=="
done

assert_not_contains '/opt/skills-venv'
assert_not_contains 'mirrors\.aliyun\.com'
assert_not_contains 'jupyter'

printf 'static contract passed\n'

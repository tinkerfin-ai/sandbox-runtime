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

assert_before() {
    local relative_path=$1
    local first_text=$2
    local second_text=$3
    local first_line
    local second_line

    first_line=$(rg --fixed-strings --line-number --max-count 1 -- \
        "${first_text}" "${REPO_ROOT}/${relative_path}" | cut -d: -f1)
    second_line=$(rg --fixed-strings --line-number --max-count 1 -- \
        "${second_text}" "${REPO_ROOT}/${relative_path}" | cut -d: -f1)
    [[ -n ${first_line} && -n ${second_line} && ${first_line} -lt ${second_line} ]] \
        || fail "${relative_path} must place ${first_text} before ${second_text}"
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
[[ ${NPM_VERSION} =~ ^12\.0\.[0-9]+$ ]] || fail "npm must stay on 12.0"
npm_patch=${NPM_VERSION##*.}
((10#${npm_patch} >= 2)) || fail "npm must include security fixes from 12.0.2"
[[ ${GO_VERSION} =~ ^1\.25\.[0-9]+$ ]] || fail "Go must stay on 1.25"
go_patch=${GO_VERSION##*.}
((10#${go_patch} >= 12)) || fail "Go must include security fixes from 1.25.12"
[[ ${MAVEN_VERSION} =~ ^3\.9\.[0-9]+$ ]] || fail "Maven must stay on 3.9"
[[ ${SETUPTOOLS_VERSION:-} =~ ^84\.0\.[0-9]+$ ]] \
    || fail "setuptools must include fixed vendored dependencies from 84.0"
[[ ${NPM_BRACE_EXPANSION_VERSION:-} == 5.0.9 ]] \
    || fail "npm brace-expansion must include security fixes from 5.0.9"
[[ ${NPM_IP_ADDRESS_VERSION:-} == 10.3.1 ]] \
    || fail "npm ip-address must include security fixes from 10.3.1"
[[ ${NPM_SHA512} =~ ^[0-9a-f]{128}$ ]] || fail "npm SHA-512 is invalid"
[[ ${NPM_BRACE_EXPANSION_SHA512:-} =~ ^[0-9a-f]{128}$ ]] \
    || fail "brace-expansion SHA-512 is invalid"
[[ ${NPM_IP_ADDRESS_SHA512:-} =~ ^[0-9a-f]{128}$ ]] \
    || fail "ip-address SHA-512 is invalid"
[[ ${SETUPTOOLS_SHA256:-} =~ ^[0-9a-f]{64}$ ]] \
    || fail "setuptools SHA-256 is invalid"
[[ ${GO_SHA256_AMD64} =~ ^[0-9a-f]{64}$ ]] || fail "Go amd64 SHA-256 is invalid"
[[ ${GO_SHA256_ARM64} =~ ^[0-9a-f]{64}$ ]] || fail "Go arm64 SHA-256 is invalid"

assert_contains Dockerfile 'VIRTUAL_ENV=/opt/sandbox-runtime/venv'
assert_contains Dockerfile 'JAVA_HOME=/opt/sandbox-runtime/jdk'
assert_contains Dockerfile 'GOROOT=/opt/sandbox-runtime/go'
assert_contains Dockerfile 'MAVEN_HOME=/opt/sandbox-runtime/maven'
assert_contains Dockerfile 'setuptools.*\.whl'
assert_contains Dockerfile 'node_modules/brace-expansion'
assert_contains Dockerfile 'node_modules/ip-address'
assert_contains Dockerfile 'ENTRYPOINT \["/opt/sandbox-runtime/bin/entrypoint.sh"\]'
assert_contains apt.conf 'Acquire::Retries "5";'
assert_contains .github/workflows/ci.yml 'linux/amd64,linux/arm64'
assert_contains .github/workflows/ci.yml 'Report high and critical vulnerabilities'
assert_contains .github/workflows/ci.yml 'Block fixable high and critical vulnerabilities'
assert_contains .github/workflows/ci.yml 'severity: CRITICAL,HIGH'
assert_contains .github/workflows/release.yml 'ghcr.io/tinkerfin-ai/sandbox-runtime'
assert_contains .github/workflows/release.yml 'Verify anonymous image access'
assert_contains .github/workflows/release.yml 'Report high and critical vulnerabilities'
assert_contains .github/workflows/release.yml 'Block fixable high and critical vulnerabilities'
assert_contains .github/workflows/release.yml 'severity: CRITICAL,HIGH'
assert_contains .github/workflows/release.yml 'push-by-digest=true'
assert_contains .github/workflows/release.yml '^  publish:'
assert_contains .github/workflows/release.yml 'needs: build'
assert_contains .github/workflows/release.yml 'Upload image digest'
assert_contains .github/workflows/release.yml 'Create and publish OCI index'
assert_contains .github/workflows/release.yml 'platform: linux/amd64'
assert_contains .github/workflows/release.yml 'platform: linux/arm64'
assert_contains .github/workflows/release.yml '^concurrency:'
assert_contains .github/workflows/release.yml 'cancel-in-progress: false'
assert_contains .github/workflows/release.yml 'Refuse to overwrite exact version tag'
assert_contains .github/workflows/release.yml 'unexpected OCI index platforms'
assert_before .github/workflows/release.yml \
    'Refuse to overwrite exact version tag' 'Build and push platform digest'
assert_before .github/workflows/release.yml \
    'Block fixable high and critical vulnerabilities' 'Upload image digest'
assert_before .github/workflows/release.yml \
    'Upload image digest' 'Create and publish OCI index'
for workflow in .github/workflows/ci.yml .github/workflows/release.yml; do
    [[ $(rg --count 'severity: CRITICAL,HIGH' "${REPO_ROOT}/${workflow}") -eq 2 ]] \
        || fail "${workflow} must report and gate high and critical vulnerabilities"
    [[ $(rg --count 'ignore-unfixed: false' "${REPO_ROOT}/${workflow}") -eq 1 ]] \
        || fail "${workflow} must report vulnerabilities without an upstream fix"
    [[ $(rg --count 'ignore-unfixed: true' "${REPO_ROOT}/${workflow}") -eq 1 ]] \
        || fail "${workflow} must gate vulnerabilities with an upstream fix"
    [[ $(rg --count 'scanners: vuln' "${REPO_ROOT}/${workflow}") -eq 2 ]] \
        || fail "${workflow} vulnerability steps must use the vulnerability scanner"
    [[ $(rg --count 'vuln-type: os,library' "${REPO_ROOT}/${workflow}") -eq 2 ]] \
        || fail "${workflow} vulnerability steps must scan OS and language packages"
    [[ $(rg --count 'exit-code: "0"' "${REPO_ROOT}/${workflow}") -eq 1 ]] \
        || fail "${workflow} must keep one non-blocking vulnerability report"
    [[ $(rg --count 'exit-code: "1"' "${REPO_ROOT}/${workflow}") -eq 1 ]] \
        || fail "${workflow} must keep one blocking vulnerability scan"
    [[ $(rg --count 'version: v0\.70\.0' "${REPO_ROOT}/${workflow}") -eq 2 ]] \
        || fail "${workflow} vulnerability steps must pin Trivy 0.70.0"
done
if rg --quiet 'visibility=public|Make package public' \
    "${REPO_ROOT}/.github/workflows/release.yml"; then
    fail "release workflow must not rely on unsupported package visibility API"
fi

runtime_install_line=$(rg --line-number --max-count 1 \
    '^RUN --mount=type=bind,from=downloads,source=/tmp/setuptools\.whl' \
    "${REPO_ROOT}/Dockerfile" | cut -d: -f1)
image_label_line=$(rg --line-number --max-count 1 \
    '^LABEL org\.opencontainers\.image\.title' "${REPO_ROOT}/Dockerfile" | cut -d: -f1)
[[ ${runtime_install_line} -lt ${image_label_line} ]] \
    || fail "volatile image labels must follow runtime installation for cache reuse"

for package in numpy pandas matplotlib requests beautifulsoup4; do
    assert_contains requirements.in "^${package}=="
done

assert_not_contains '/opt/skills-venv'
assert_not_contains 'mirrors\.aliyun\.com'
assert_not_contains 'jupyter'

printf 'static contract passed\n'

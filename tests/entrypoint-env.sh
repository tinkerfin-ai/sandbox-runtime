#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly ENTRYPOINT="${REPO_ROOT}/scripts/entrypoint.sh"
TEST_ROOT="$(mktemp -d)"
readonly TEST_ROOT
readonly EXECD_ENV_FILE="${TEST_ROOT}/execd.env"

cleanup() {
    rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

fail() {
    printf 'entrypoint test failed: %s\n' "$*" >&2
    exit 1
}

[[ -x "${ENTRYPOINT}" ]] || fail "entrypoint is missing or not executable"
printf 'KEEP_ME=preserved\nPATH=stale\n' >"${EXECD_ENV_FILE}"

run_entrypoint() {
    EXECD_ENVS="${EXECD_ENV_FILE}" \
    PATH='/runtime/venv/bin:/usr/bin:/bin' \
    VIRTUAL_ENV='/runtime/venv' \
    JAVA_HOME='/runtime/jdk' \
    GOROOT='/runtime/go' \
    MAVEN_HOME='/runtime/maven' \
    MPLBACKEND='Agg' \
    LANG='C.UTF-8' \
    PIP_INDEX_URL='https://pypi.example/simple' \
        "${ENTRYPOINT}" sh -c 'printf command-executed'
}

[[ $(run_entrypoint) == 'command-executed' ]] || fail "command was not executed"
[[ $(run_entrypoint) == 'command-executed' ]] || fail "second command was not executed"

for key in PATH VIRTUAL_ENV JAVA_HOME GOROOT MAVEN_HOME MPLBACKEND LANG PIP_INDEX_URL; do
    [[ $(rg --count "^${key}=" "${EXECD_ENV_FILE}") -eq 1 ]] \
        || fail "${key} was not upserted exactly once"
done
rg --quiet '^KEEP_ME=preserved$' "${EXECD_ENV_FILE}" \
    || fail "unmanaged values were not preserved"

if EXECD_ENVS="${EXECD_ENV_FILE}" \
    PIP_INDEX_URL=$'https://pypi.example/simple\nINJECTED=yes' \
    "${ENTRYPOINT}" true 2>/dev/null; then
    fail "newline environment injection was accepted"
fi
if rg --quiet '^INJECTED=' "${EXECD_ENV_FILE}"; then
    fail "newline environment injection reached the env file"
fi

printf 'entrypoint contract passed\n'

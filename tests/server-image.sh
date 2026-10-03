#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_REF=${1:?usage: bash tests/server-image.sh IMAGE}
RUN_ID=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
CONTAINER_NAME="tinkerfin-server-test-${RUN_ID}"
readonly REPO_ROOT IMAGE_REF RUN_ID CONTAINER_NAME
docker_pid=

cleanup() {
    local test_status=$?
    trap - EXIT
    trap '' INT TERM
    docker_pid=${docker_pid:-${!:-}}
    if [[ -n ${docker_pid} ]]; then
        kill -TERM "${docker_pid}" 2>/dev/null || true
    fi
    if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        local owner
        owner=$(docker inspect --format '{{index .Config.Labels "io.tinkerfin.server-test"}}' "${CONTAINER_NAME}")
        if [[ ${owner} != "${RUN_ID}" ]]; then
            printf 'refusing to remove container with changed ownership: %s\n' "${CONTAINER_NAME}" >&2
            exit 1
        fi
        docker rm --force --volumes "${CONTAINER_NAME}" >/dev/null || exit 1
    fi
    if [[ -n ${docker_pid} ]]; then
        wait "${docker_pid}" 2>/dev/null || true
    fi
    local remaining
    remaining=$(docker ps --all --quiet --filter "label=io.tinkerfin.server-test=${RUN_ID}")
    [[ -z ${remaining} ]] || exit 1
    printf 'server image integration cleanup: %s; remaining=[]\n' "${RUN_ID}"
    exit "${test_status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'server image integration run: %s\n' "${RUN_ID}"
docker image inspect "${IMAGE_REF}" >/dev/null
docker create --pull never --name "${CONTAINER_NAME}" --interactive --network none \
    --label "io.tinkerfin.server-test=${RUN_ID}" \
    --entrypoint python "${IMAGE_REF}" - >/dev/null
docker start --attach --interactive "${CONTAINER_NAME}" \
    <"${REPO_ROOT}/tests/server-contract.py" &
docker_pid=$!
wait "${docker_pid}"

#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_REF=${1:?usage: bash tests/workspace-runtime.sh IMAGE_REF}
RUN_ID=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
CONTAINER_NAME="tinkerfin-workspace-test-${RUN_ID}"
readonly REPO_ROOT IMAGE_REF RUN_ID CONTAINER_NAME
test_process=

cleanup() {
    local result=$?
    trap - EXIT
    trap '' INT TERM
    test_process=${test_process:-${!:-}}
    if [[ -n ${test_process} ]]; then
        kill -TERM "${test_process}" 2>/dev/null || true
    fi
    if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        local owner
        owner=$(docker inspect --format '{{index .Config.Labels "io.tinkerfin.workspace-test"}}' "${CONTAINER_NAME}")
        [[ ${owner} == "${RUN_ID}" ]] || exit 1
        docker rm --force --volumes "${CONTAINER_NAME}" >/dev/null || exit 1
    fi
    if [[ -n ${test_process} ]]; then
        wait "${test_process}" 2>/dev/null || true
    fi
    local remaining
    remaining=$(docker ps --all --quiet --filter "label=io.tinkerfin.workspace-test=${RUN_ID}")
    [[ -z ${remaining} ]] || exit 1
    printf 'workspace runtime cleanup: %s; remaining=[]\n' "${RUN_ID}"
    exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'workspace runtime run: %s\n' "${RUN_ID}"
docker image inspect "${IMAGE_REF}" >/dev/null
docker create --pull never --name "${CONTAINER_NAME}" --network none \
    --env "WORKSPACE_TEST_SIGNAL_CHECK=${WORKSPACE_TEST_SIGNAL_CHECK:-0}" \
    --label "io.tinkerfin.workspace-test=${RUN_ID}" --entrypoint /bin/bash \
    "${IMAGE_REF}" -Eeuo pipefail -c '
        test -d /opt/sandbox-runtime/workspaces
        ln -s /opt/sandbox-runtime/workspaces /tmp/workspace-runtime
        if [[ ${WORKSPACE_TEST_SIGNAL_CHECK} == 1 ]]; then
            exec python -u -I -S -c \
                "import signal; print(\"workspace runtime signal-ready\", flush=True); signal.pause()"
        fi
        PYTHONPATH=/opt/sandbox-runtime/workspaces \
            python -m unittest discover -s /tmp/workspace-contract \
                -p "test_workspace_*.py" -v
    ' >/dev/null
docker cp "${REPO_ROOT}/tests/." "${CONTAINER_NAME}:/tmp/workspace-contract"
docker start --attach "${CONTAINER_NAME}" &
test_process=$!
wait "${test_process}"
test_process=

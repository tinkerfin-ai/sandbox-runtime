#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
EXECD_IMAGE=${1:?usage: bash opensandbox-execd/tests/image.sh EXECD_IMAGE [EXECD_BINARY]}
EXECD_BINARY=${2:-}
RUNTIME_IMAGE=${EXECD_RUNTIME_IMAGE:-ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.2}
RUN_ID=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
SOURCE_NAME="tinkerfin-execd-source-${RUN_ID}"
RUNTIME_NAME="tinkerfin-execd-integration-${RUN_ID}"
TASK_DIRECTORY=$(mktemp -d)
TEST_PROCESS=
readonly REPO_ROOT EXECD_IMAGE EXECD_BINARY RUNTIME_IMAGE RUN_ID SOURCE_NAME RUNTIME_NAME TASK_DIRECTORY

cleanup() {
    local result=$?
    trap - EXIT
    trap '' INT TERM
    TEST_PROCESS=${TEST_PROCESS:-${!:-}}
    if [[ -n ${TEST_PROCESS} ]]; then
        kill -TERM "${TEST_PROCESS}" 2>/dev/null || true
    fi
    local name owner
    for name in "${RUNTIME_NAME}" "${SOURCE_NAME}"; do
        if docker inspect "${name}" >/dev/null 2>&1; then
            owner=$(docker inspect --format '{{index .Config.Labels "io.tinkerfin.execd-test"}}' "${name}")
            [[ ${owner} == "${RUN_ID}" ]] || exit 1
            docker rm --force --volumes "${name}" >/dev/null || exit 1
        fi
    done
    if [[ -n ${TEST_PROCESS} ]]; then wait "${TEST_PROCESS}" || true; fi
    local remaining
    remaining=$(docker ps --all --quiet --filter "label=io.tinkerfin.execd-test=${RUN_ID}")
    [[ -z ${remaining} ]] || exit 1
    rm -rf -- "${TASK_DIRECTORY}"
    printf 'execd integration cleanup: %s; remaining=[]\n' "${RUN_ID}"
    exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'execd integration run: %s\n' "${RUN_ID}"
docker image inspect "${EXECD_IMAGE}" "${RUNTIME_IMAGE}" >/dev/null
docker create --pull never --name "${SOURCE_NAME}" --label "io.tinkerfin.execd-test=${RUN_ID}" \
    --network none "${EXECD_IMAGE}" >/dev/null
docker cp "${SOURCE_NAME}:/execd" "${TASK_DIRECTORY}/execd"
docker cp "${SOURCE_NAME}:/usr/local/bin/bwrap" "${TASK_DIRECTORY}/bwrap"
docker cp "${SOURCE_NAME}:/opt/opensandbox/opensandbox-session-gate" "${TASK_DIRECTORY}/opensandbox-session-gate"
docker create --pull never --name "${RUNTIME_NAME}" --label "io.tinkerfin.execd-test=${RUN_ID}" \
    --network none --cap-add SYS_ADMIN --cap-add NET_ADMIN \
    --security-opt no-new-privileges:true --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined --pids-limit 128 --memory 512m \
    --tmpfs /var/lib/execd/isolation --entrypoint /usr/bin/sleep \
    "${RUNTIME_IMAGE}" infinity >/dev/null
docker start "${RUNTIME_NAME}" >/dev/null
docker exec "${RUNTIME_NAME}" mkdir -p /opt/opensandbox
docker cp "${TASK_DIRECTORY}/bwrap" "${RUNTIME_NAME}:/usr/local/bin/bwrap"
docker cp "${TASK_DIRECTORY}/opensandbox-session-gate" "${RUNTIME_NAME}:/opt/opensandbox/opensandbox-session-gate"
docker exec "${RUNTIME_NAME}" chown 0:0 /opt/opensandbox/opensandbox-session-gate
docker exec "${RUNTIME_NAME}" chmod 0555 /opt/opensandbox/opensandbox-session-gate
if [[ -n ${EXECD_BINARY} ]]; then
    docker cp "${EXECD_BINARY}" "${RUNTIME_NAME}:/usr/local/bin/execd"
else
    docker cp "${TASK_DIRECTORY}/execd" "${RUNTIME_NAME}:/usr/local/bin/execd"
fi
docker cp "${REPO_ROOT}/opensandbox-execd/tests/native-lifecycle.py" "${RUNTIME_NAME}:/tmp/native-lifecycle.py"
if [[ ${EXECD_TEST_SIGNAL_CHECK:-0} == 1 ]]; then
    docker exec "${RUNTIME_NAME}" python3 -u -I -S -c \
        'import signal; print("execd integration signal-ready", flush=True); signal.pause()' &
else
    docker exec "${RUNTIME_NAME}" python3 -I -S /tmp/native-lifecycle.py &
fi
TEST_PROCESS=$!
wait "${TEST_PROCESS}"
TEST_PROCESS=

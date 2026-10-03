#!/usr/bin/env bash

set -Eeuo pipefail

SOURCE_ROOT=${1:?usage: bash opensandbox-execd/tests/linux.sh UPSTREAM_SOURCE_ROOT [EXECD_IMAGE]}
EXECD_IMAGE=${2:-opensandbox-execd:dev}
RUNTIME_IMAGE=${EXECD_RUNTIME_IMAGE:-ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.2}
RUN_ID=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
SOURCE_NAME="tinkerfin-execd-unit-source-${RUN_ID}"
RUNTIME_NAME="tinkerfin-execd-linux-${RUN_ID}"
EXECD_ARCH=$(docker image inspect "${RUNTIME_IMAGE}" --format '{{.Architecture}}')
ISOLATION_FILTER=${EXECD_ISOLATION_FILTER:-'^Test(Namespace|MergedView|Lifecycle|BwrapStatus|BwrapLifecycle|SessionGate|OpenSessionGate|UpperManager)'}
RUNTIME_FILTER=${EXECD_RUNTIME_FILTER:-"^($(awk '/^func Test/ {sub(/\(.*/, "", $2); print $2}' "${SOURCE_ROOT}/components/execd/pkg/runtime/isolated_"*test.go | paste -sd '|' -))$"}
NATIVE_RUNTIME_FILTER=${EXECD_NATIVE_RUNTIME_FILTER:-'^Test(PrivateSessionPinsNamespacesBeforeMarkReady|DeclaredSessionCancelBeforeNativeReady|PrivateDevicesAfterRootBind)$'}
BWRAP_FILTER=${EXECD_BWRAP_FILTER:-'.'}
TASK_DIRECTORY=$(mktemp -d)
TEST_PROCESS=
TEST_RESULT=0
readonly SOURCE_ROOT EXECD_IMAGE RUNTIME_IMAGE RUN_ID SOURCE_NAME RUNTIME_NAME TASK_DIRECTORY EXECD_ARCH ISOLATION_FILTER RUNTIME_FILTER NATIVE_RUNTIME_FILTER BWRAP_FILTER

cleanup() {
    local result=$1
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
    printf 'execd Linux cleanup: %s; remaining=[]\n' "${RUN_ID}"
    exit "${result}"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'execd Linux run: %s\n' "${RUN_ID}"
docker image inspect "${EXECD_IMAGE}" >/dev/null
for package in isolation runtime; do
    (
        cd "${SOURCE_ROOT}/components/execd"
        GOTOOLCHAIN=go1.25.13 GOOS=linux GOARCH="${EXECD_ARCH}" go test -c -o "${TASK_DIRECTORY}/${package}.test" "./pkg/${package}"
    )
done
(
    cd "${SOURCE_ROOT}/components/execd"
    GOTOOLCHAIN=go1.25.13 GOOS=linux GOARCH="${EXECD_ARCH}" go test -tags=bwrap -c \
        -o "${TASK_DIRECTORY}/runtime-native.test" ./pkg/runtime
    GOTOOLCHAIN=go1.25.13 GOOS=linux GOARCH="${EXECD_ARCH}" go test -tags=bwrap -c \
        -o "${TASK_DIRECTORY}/bwrap.test" ./pkg/runtime/bwrap_test
)
docker create --pull never --name "${SOURCE_NAME}" --label "io.tinkerfin.execd-test=${RUN_ID}" --network none "${EXECD_IMAGE}" >/dev/null
docker cp "${SOURCE_NAME}:/usr/local/bin/bwrap" "${TASK_DIRECTORY}/bwrap"
docker cp "${SOURCE_NAME}:/opt/opensandbox/opensandbox-session-gate" "${TASK_DIRECTORY}/opensandbox-session-gate"
docker create --pull never --init --name "${RUNTIME_NAME}" --label "io.tinkerfin.execd-test=${RUN_ID}" \
    --network none --cap-add SYS_ADMIN --cap-add NET_ADMIN \
    --security-opt no-new-privileges:true --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined --pids-limit 128 --memory 512m \
    --tmpfs /tmp:exec --tmpfs /var/lib/execd/isolation --entrypoint /usr/bin/sleep \
    "${RUNTIME_IMAGE}" infinity >/dev/null
docker start "${RUNTIME_NAME}" >/dev/null
docker exec "${RUNTIME_NAME}" mkdir -p /opt/opensandbox /tests/pkg/isolation /tests/pkg/runtime /tests/native
docker cp "${TASK_DIRECTORY}/bwrap" "${RUNTIME_NAME}:/usr/local/bin/bwrap"
docker cp "${TASK_DIRECTORY}/opensandbox-session-gate" "${RUNTIME_NAME}:/opt/opensandbox/opensandbox-session-gate"
docker exec "${RUNTIME_NAME}" chown 0:0 /opt/opensandbox/opensandbox-session-gate
docker exec "${RUNTIME_NAME}" chmod 0555 /opt/opensandbox/opensandbox-session-gate
docker cp "${SOURCE_ROOT}/components/execd/native/." "${RUNTIME_NAME}:/tests/native/"
for package in isolation runtime runtime-native bwrap; do
    docker cp "${TASK_DIRECTORY}/${package}.test" "${RUNTIME_NAME}:/tests/${package}.test"
done
docker exec --workdir /tests/pkg/isolation "${RUNTIME_NAME}" /tests/isolation.test \
    -test.run "${ISOLATION_FILTER}" -test.v -test.timeout 120s &
TEST_PROCESS=$!
wait "${TEST_PROCESS}" || TEST_RESULT=1
TEST_PROCESS=
docker exec --user 1000:1000 --workdir /tests/pkg/runtime "${RUNTIME_NAME}" /tests/runtime.test \
    -test.run "${RUNTIME_FILTER}" \
    -test.v -test.timeout 120s &
TEST_PROCESS=$!
wait "${TEST_PROCESS}" || TEST_RESULT=1
TEST_PROCESS=
docker exec --workdir /tests/pkg/runtime "${RUNTIME_NAME}" /tests/runtime-native.test \
    -test.run "${NATIVE_RUNTIME_FILTER}" -test.v -test.timeout 120s &
TEST_PROCESS=$!
wait "${TEST_PROCESS}" || TEST_RESULT=1
TEST_PROCESS=
docker exec --workdir /tests/pkg/runtime "${RUNTIME_NAME}" /tests/bwrap.test \
    -test.run "${BWRAP_FILTER}" -test.v -test.timeout 120s &
TEST_PROCESS=$!
wait "${TEST_PROCESS}" || TEST_RESULT=1
TEST_PROCESS=
cleanup "${TEST_RESULT}"

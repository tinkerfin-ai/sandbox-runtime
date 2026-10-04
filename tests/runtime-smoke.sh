#!/usr/bin/env bash

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

readonly IMAGE_REF=${1:?usage: tests/runtime-smoke.sh IMAGE_REF}
readonly MAX_UNPACKED_BYTES=${MAX_UNPACKED_BYTES:-2500000000}
RUN_ID=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
readonly RUN_ID
readonly ENVIRONMENT_NAME="tinkerfin-runtime-environment-${RUN_ID}"
readonly TOOLCHAINS_NAME="tinkerfin-runtime-toolchains-${RUN_ID}"
readonly BROWSER_NAME="tinkerfin-runtime-browser-${RUN_ID}"
test_process=

cleanup() {
    local result=$?
    trap - EXIT
    trap '' INT TERM
    test_process=${test_process:-${!:-}}
    if [[ -n ${test_process} ]]; then
        kill -TERM "${test_process}" 2>/dev/null || true
    fi
    local name owner
    for name in "${ENVIRONMENT_NAME}" "${TOOLCHAINS_NAME}" "${BROWSER_NAME}"; do
        if docker container inspect "${name}" >/dev/null 2>&1; then
            owner=$(docker inspect --format '{{index .Config.Labels "io.tinkerfin.runtime-test"}}' "${name}")
            [[ ${owner} == "${RUN_ID}" ]] || exit 1
            docker rm --force --volumes "${name}" >/dev/null || exit 1
        fi
    done
    if [[ -n ${test_process} ]]; then
        wait "${test_process}" 2>/dev/null || true
    fi
    local remaining
    remaining=$(docker ps --all --quiet --filter "label=io.tinkerfin.runtime-test=${RUN_ID}")
    [[ -z ${remaining} ]] || exit 1
    printf 'runtime smoke cleanup: %s; remaining=[]\n' "${RUN_ID}"
    exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_container() {
    local name=$1
    shift
    docker create --pull never --name "${name}" --interactive --network none \
        --label "io.tinkerfin.runtime-test=${RUN_ID}" "$@" >/dev/null
    docker start --attach --interactive "${name}" <&0 &
    test_process=$!
    wait "${test_process}"
    test_process=
}

printf 'runtime smoke run: %s\n' "${RUN_ID}"
image_size=$(docker image inspect --format '{{.Size}}' "${IMAGE_REF}")
if ((image_size > MAX_UNPACKED_BYTES)); then
    printf 'image is too large: %s bytes (limit %s)\n' \
        "${image_size}" "${MAX_UNPACKED_BYTES}" >&2
    exit 1
fi

if [[ ${RUNTIME_TEST_SIGNAL_CHECK:-0} == 1 ]]; then
    run_container "${ENVIRONMENT_NAME}" --entrypoint python "${IMAGE_REF}" \
        -u -I -S -c 'import signal; print("runtime smoke signal-ready", flush=True); signal.pause()'
    exit 0
fi

run_container "${ENVIRONMENT_NAME}" \
    --env EXECD_ENVS=/tmp/execd.env \
    "${IMAGE_REF}" bash -Eeuo pipefail <<'BASH'
    test -f /tmp/execd.env
    test "$(rg --count "^PATH=" /tmp/execd.env)" -eq 1
    test "$(rg --count "^VIRTUAL_ENV=" /tmp/execd.env)" -eq 1
    rg --quiet "^VIRTUAL_ENV=/opt/sandbox-runtime/venv$" /tmp/execd.env
    rg --quiet "^JAVA_HOME=/opt/sandbox-runtime/jdk$" /tmp/execd.env
    rg --quiet "^NODE_HOME=/opt/sandbox-runtime/node$" /tmp/execd.env
    rg --quiet "^GOROOT=/opt/sandbox-runtime/go$" /tmp/execd.env
    rg --quiet "^MAVEN_HOME=/opt/sandbox-runtime/maven$" /tmp/execd.env
    rg --quiet "^PLAYWRIGHT_BROWSERS_PATH=/opt/sandbox-runtime/browsers$" /tmp/execd.env
BASH

run_container "${TOOLCHAINS_NAME}" "${IMAGE_REF}" bash -Eeuo pipefail <<'BASH'
    test "${VIRTUAL_ENV}" = /opt/sandbox-runtime/venv
    test "${JAVA_HOME}" = /opt/sandbox-runtime/jdk
    test "${GOROOT}" = /opt/sandbox-runtime/go
    test "${MAVEN_HOME}" = /opt/sandbox-runtime/maven
    test "$(command -v python)" = /opt/sandbox-runtime/venv/bin/python
    test "$(command -v pip)" = /opt/sandbox-runtime/venv/bin/pip
    test ! -e /opt/skills-venv
    ! command -v jupyter

    python - <<"PY"
import bs4
import matplotlib
import matplotlib.pyplot as plt
import numpy
import pandas
import requests

assert matplotlib.get_backend().lower() == "agg"
plt.plot([1, 2], [3, 4])
plt.savefig("/tmp/runtime-smoke.png")
print(numpy.__version__, pandas.__version__, requests.__version__, bs4.__version__)
PY
    test -s /tmp/runtime-smoke.png

    printf "public class Hello { public static void main(String[] args) { System.out.print(\"java-ok\"); } }" >/tmp/Hello.java
    javac /tmp/Hello.java
    test "$(java -cp /tmp Hello)" = java-ok
    [[ $(mvn --version) == *"Apache Maven 3.9.9"* ]]
    test ! -e /root/.m2/repository

    test "$(node -e "process.stdout.write(\"node-ok\")")" = node-ok
    test "$(npm --version)" = 12.0.2
    test "$(node -p \
        "require(\"/opt/sandbox-runtime/node/lib/node_modules/npm/node_modules/brace-expansion/package.json\").version")" \
        = 5.0.12
    test "$(node -p \
        "require(\"/opt/sandbox-runtime/node/lib/node_modules/npm/node_modules/undici/package.json\").version")" \
        = 6.28.1
    test "$(node -p \
        "require(\"/opt/sandbox-runtime/node/lib/node_modules/npm/node_modules/ip-address/package.json\").version")" \
        = 10.3.1
    test "$(node -p \
        "require(\"/opt/sandbox-runtime/node/lib/node_modules/npm/node_modules/tar/package.json\").version")" \
        = 7.5.21
    node - <<"JS"
const assert = require("node:assert/strict");
const npmModules = "/opt/sandbox-runtime/node/lib/node_modules/npm/node_modules";
const { minimatch } = require(`${npmModules}/minimatch`);
const { Address4, Address6 } = require(`${npmModules}/ip-address`);

assert.equal(minimatch("src/main.js", "src/*.{js,ts}"), true);
assert.equal(new Address4("127.0.0.1").isCorrect(), true);
assert.equal(new Address6("::1").isCorrect(), true);
JS
    mkdir /tmp/npm-smoke
    printf "%s\n" \
        "{\"name\":\"npm-smoke\",\"version\":\"1.0.0\"}" \
        >/tmp/npm-smoke/package.json
    (cd /tmp/npm-smoke && npm pack --ignore-scripts --silent >/tmp/npm-pack-output.txt)
    test -s /tmp/npm-smoke/npm-smoke-1.0.0.tgz

    test "$(python -c "import setuptools; print(setuptools.__version__)")" = 84.0.0
    test "$(python -c "import urllib3; print(urllib3.__version__)")" = 2.8.0
    test "$(/usr/local/bin/python -c \
        "import setuptools; print(setuptools.__version__)")" = 84.0.0

    printf "%s\\n" \
        "package main" \
        "import \"fmt\"" \
        "func main(){fmt.Print(\"go-ok\")}" >/tmp/main.go
    go build -o /tmp/go-smoke /tmp/main.go
    test "$(/tmp/go-smoke)" = go-ok

    printf "%s\\n" \
        "#include <stdio.h>" \
        "int main(void){fputs(\"c-ok\", stdout);return 0;}" >/tmp/main.c
    cc /tmp/main.c -o /tmp/c-smoke
    test "$(/tmp/c-smoke)" = c-ok
BASH

docker create --pull never --name "${BROWSER_NAME}" --interactive --network none \
    --label "io.tinkerfin.runtime-test=${RUN_ID}" "${IMAGE_REF}" python - >/dev/null
docker start --attach --interactive "${BROWSER_NAME}" \
    <"${REPO_ROOT}/tests/browser-smoke.py" &
test_process=$!
wait "${test_process}"
test_process=

printf 'runtime smoke passed for %s (%s bytes)\n' "${IMAGE_REF}" "${image_size}"

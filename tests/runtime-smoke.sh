#!/usr/bin/env bash

set -Eeuo pipefail

readonly IMAGE_REF=${1:?usage: tests/runtime-smoke.sh IMAGE_REF}
readonly MAX_UNPACKED_BYTES=${MAX_UNPACKED_BYTES:-2000000000}

container_id=

cleanup() {
    if [[ -n ${container_id} ]]; then
        docker rm --force "${container_id}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

image_size=$(docker image inspect --format '{{.Size}}' "${IMAGE_REF}")
if ((image_size > MAX_UNPACKED_BYTES)); then
    printf 'image is too large: %s bytes (limit %s)\n' \
        "${image_size}" "${MAX_UNPACKED_BYTES}" >&2
    exit 1
fi

container_id=$(docker run --detach \
    --env EXECD_ENVS=/tmp/execd.env \
    "${IMAGE_REF}")
[[ $(docker inspect --format '{{.State.Running}}' "${container_id}") == true ]]
docker exec "${container_id}" bash -Eeuo pipefail -c '
    test "$(rg --count "^PATH=" /tmp/execd.env)" -eq 1
    test "$(rg --count "^VIRTUAL_ENV=" /tmp/execd.env)" -eq 1
    rg --quiet "^VIRTUAL_ENV=/opt/sandbox-runtime/venv$" /tmp/execd.env
    rg --quiet "^JAVA_HOME=/opt/sandbox-runtime/jdk$" /tmp/execd.env
    rg --quiet "^NODE_HOME=/opt/sandbox-runtime/node$" /tmp/execd.env
    rg --quiet "^GOROOT=/opt/sandbox-runtime/go$" /tmp/execd.env
    rg --quiet "^MAVEN_HOME=/opt/sandbox-runtime/maven$" /tmp/execd.env
'
docker rm --force "${container_id}" >/dev/null
container_id=

docker run --rm "${IMAGE_REF}" bash -Eeuo pipefail -c '
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
    npm --version >/dev/null

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
'

printf 'runtime smoke passed for %s (%s bytes)\n' "${IMAGE_REF}" "${image_size}"

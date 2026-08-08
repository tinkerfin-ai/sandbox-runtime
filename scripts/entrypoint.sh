#!/usr/bin/env bash

set -Eeuo pipefail

readonly MANAGED_ENV_KEYS=(
    PATH
    VIRTUAL_ENV
    JAVA_HOME
    NODE_HOME
    GOROOT
    MAVEN_HOME
    MPLBACKEND
    LANG
    LC_ALL
    PIP_INDEX_URL
    NPM_CONFIG_REGISTRY
    GOPROXY
)

is_managed_key() {
    local candidate=$1
    local managed_key

    for managed_key in "${MANAGED_ENV_KEYS[@]}"; do
        [[ ${candidate} == "${managed_key}" ]] && return 0
    done
    return 1
}

sync_execd_envs() {
    [[ -n ${EXECD_ENVS:-} ]] || return 0

    if [[ ${EXECD_ENVS} == *$'\n'* || ${EXECD_ENVS} == *$'\r'* ]]; then
        printf 'invalid EXECD_ENVS path: newlines are not allowed\n' >&2
        return 1
    fi
    if [[ -e ${EXECD_ENVS} && ! -f ${EXECD_ENVS} ]]; then
        printf 'invalid EXECD_ENVS path: expected a regular file\n' >&2
        return 1
    fi

    local managed_key
    local managed_value
    for managed_key in "${MANAGED_ENV_KEYS[@]}"; do
        declare -p "${managed_key}" >/dev/null 2>&1 || continue
        managed_value=${!managed_key}
        if [[ ${managed_value} == *$'\n'* || ${managed_value} == *$'\r'* ]]; then
            printf 'invalid %s value: newlines are not allowed\n' "${managed_key}" >&2
            return 1
        fi
    done

    local env_directory
    local temporary_file
    env_directory=$(dirname -- "${EXECD_ENVS}")
    temporary_file=$(mktemp "${env_directory}/.execd-env.XXXXXX")

    if [[ -f ${EXECD_ENVS} ]]; then
        local existing_key
        local existing_line
        while IFS= read -r existing_line || [[ -n ${existing_line} ]]; do
            existing_key=${existing_line%%=*}
            if ! is_managed_key "${existing_key}"; then
                printf '%s\n' "${existing_line}" >>"${temporary_file}"
            fi
        done <"${EXECD_ENVS}"
    fi

    for managed_key in "${MANAGED_ENV_KEYS[@]}"; do
        declare -p "${managed_key}" >/dev/null 2>&1 || continue
        printf '%s=%s\n' "${managed_key}" "${!managed_key}" >>"${temporary_file}"
    done

    chmod 0644 "${temporary_file}"
    mv -f -- "${temporary_file}" "${EXECD_ENVS}"
}

sync_execd_envs

if (($# > 0)); then
    exec "$@"
fi
exec sleep infinity

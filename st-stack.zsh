#!/usr/bin/env zsh

setopt EXTENDED_GLOB NO_NOMATCH PIPE_FAIL
set -u

umask 077

typeset -gr SCRIPT_PATH=${0:A}
typeset -gr SCRIPT_DIR=${SCRIPT_PATH:h}

typeset -gr COBOL_DIR="${HOME}/git/cobolcpp"
typeset -gr COBOL_COMMAND="./start_HQ.sh"
typeset -gr SILLY_DIR="${HOME}/git/SillyTavern"
typeset -gr ALLTALK_DIR="${HOME}/git/alltalk_tts"
typeset -gr ALLTALK_COMMAND="${HOME}/.dotfiles/config/dotconfig/scripts/start_all_talk.sh"
typeset -gr PROXY_COMMAND="./.venv/bin/st-vram-proxy"
typeset -gr KOBOLD_URL="${ST_PROXY_KOBOLD_URL:-http://127.0.0.1:5001}"
typeset -gr COMFY_URL="${ST_PROXY_COMFY_URL:-http://127.0.0.1:8188}"

typeset -gr RUNTIME_BASE="${XDG_RUNTIME_DIR:-/tmp}"
typeset -gr RUNTIME_DIR="${RUNTIME_BASE}/st-stack-${UID}"
typeset -gr LOCK_DIR="${RUNTIME_DIR}/supervisor.lock"
typeset -gr SUPERVISOR_FILE="${RUNTIME_DIR}/supervisor.pid"

typeset -gi SHOW_CHILD_LOGS=0
typeset -gi STOP_ONLY=0
typeset -gi OWNS_LOCK=0
typeset -gi CLEANUP_STARTED=0
typeset -g START_TIMEOUT="${ST_STACK_START_TIMEOUT:-30}"
typeset -g STOP_TIMEOUT="${ST_STACK_STOP_TIMEOUT:-10}"
typeset -g PROXY_CHAT_PORT="${ST_PROXY_CHAT_PORT:-5002}"
typeset -g PROXY_IMAGE_PORT="${ST_PROXY_IMAGE_PORT:-8189}"
typeset -g KOBOLD_PORT=""
typeset -g ALLTALK_PORT="${ST_STACK_ALLTALK_PORT:-7851}"

typeset -ga MATCHED_PIDS=()
typeset -ga PORT_OWNER_PIDS=()
typeset -ga TARGET_PIDS=()
typeset -ga TARGET_PGIDS=()
typeset -g PROCESS_CWD=""
typeset -g PROCESS_COMMAND=""

log() {
    print -ru2 -- "st-stack: $*"
}

usage() {
    print -ru2 -- "Usage: ${SCRIPT_PATH:t} [--log] [--stop]"
}

parse_args() {
    local argument

    for argument in "$@"; do
        case "${argument}" in
            --log)
                SHOW_CHILD_LOGS=1
                ;;
            --stop)
                STOP_ONLY=1
                ;;
            *)
                log "unknown argument: ${argument}"
                usage
                return 2
                ;;
        esac
    done

    if [[ "${START_TIMEOUT}" != <-> ]] || (( START_TIMEOUT < 1 )); then
        log "ST_STACK_START_TIMEOUT must be a positive integer"
        return 2
    fi
    if [[ "${STOP_TIMEOUT}" != <-> ]] || (( STOP_TIMEOUT < 1 )); then
        log "ST_STACK_STOP_TIMEOUT must be a positive integer"
        return 2
    fi
    if [[ "${PROXY_CHAT_PORT}" != <-> ]] || (( PROXY_CHAT_PORT < 1 || PROXY_CHAT_PORT > 65535 )); then
        log "ST_PROXY_CHAT_PORT must be an integer between 1 and 65535"
        return 2
    fi
    if [[ "${PROXY_IMAGE_PORT}" != <-> ]] || (( PROXY_IMAGE_PORT < 1 || PROXY_IMAGE_PORT > 65535 )); then
        log "ST_PROXY_IMAGE_PORT must be an integer between 1 and 65535"
        return 2
    fi
    if [[ "${ALLTALK_PORT}" != <-> ]] || (( ALLTALK_PORT < 1 || ALLTALK_PORT > 65535 )); then
        log "ST_STACK_ALLTALK_PORT must be an integer between 1 and 65535"
        return 2
    fi
    if [[ "${KOBOLD_URL}" =~ '^https?://[^/:]+:([0-9]+)(/.*)?$' ]]; then
        KOBOLD_PORT=${match[1]}
    fi
}

prepare_runtime_dir() {
    if [[ ! -d "${RUNTIME_BASE}" || ! -w "${RUNTIME_BASE}" ]]; then
        log "runtime base is not a writable directory: ${RUNTIME_BASE}"
        return 1
    fi

    if [[ -e "${RUNTIME_DIR}" ]]; then
        if [[ ! -d "${RUNTIME_DIR}" || -L "${RUNTIME_DIR}" || ! -O "${RUNTIME_DIR}" ]]; then
            log "refusing unsafe runtime path: ${RUNTIME_DIR}"
            return 1
        fi
    elif ! mkdir -m 700 -- "${RUNTIME_DIR}"; then
        log "could not create runtime directory: ${RUNTIME_DIR}"
        return 1
    fi
}

read_process_info() {
    local pid=$1

    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    PROCESS_CWD=$(readlink -f -- "/proc/${pid}/cwd" 2>/dev/null) || return 1
    PROCESS_COMMAND=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null) || return 1
    [[ -n "${PROCESS_COMMAND}" ]]
}

process_matches_service() {
    local service=$1
    local pid=$2
    local command_lower

    read_process_info "${pid}" || return 1
    command_lower=${PROCESS_COMMAND:l}

    case "${service}" in
        cobol)
            [[ "${PROCESS_CWD}" == "${COBOL_DIR}" ]] &&
                [[ "${command_lower}" == *start_hq.sh* ||
                    "${command_lower}" == *koboldcpp* ||
                    "${command_lower}" == *cobolcpp* ]]
            ;;
        silly)
            [[ "${PROCESS_CWD}" == "${SILLY_DIR}" ]] &&
                [[ "${command_lower}" == *start.sh* ||
                    ( "${command_lower}" == *python* && "${command_lower}" == *main.py* ) ||
                    ( "${command_lower}" == *node* && "${command_lower}" == *server.js* ) ]]
            ;;
        alltalk)
            [[ "${PROCESS_CWD}" == "${ALLTALK_DIR}" ]] &&
                [[ "${command_lower}" == *start_all_talk.sh* ||
                    "${command_lower}" == *start_alltalk.sh* ||
                    ( "${command_lower}" == *python* && "${command_lower}" == *script.py* ) ]]
            ;;
        proxy)
            [[ "${PROCESS_CWD}" == "${SCRIPT_DIR}" ]] &&
                [[ "${command_lower}" == *st-vram-proxy* ]]
            ;;
        *)
            return 1
            ;;
    esac
}

find_service_pids() {
    local service=$1
    local proc_dir pid

    MATCHED_PIDS=()
    for proc_dir in /proc/<->(N); do
        [[ -O "${proc_dir}" ]] || continue
        pid=${proc_dir:t}
        (( pid == $$ )) && continue
        if process_matches_service "${service}" "${pid}"; then
            MATCHED_PIDS+=("${pid}")
        fi
    done

    (( ${#MATCHED_PIDS} > 0 ))
}

service_running() {
    find_service_pids "$1"
}

kobold_ready() {
    command -v curl >/dev/null 2>&1 || return 1
    curl --silent --fail --output /dev/null \
        --connect-timeout 0.5 --max-time 1 \
        "${KOBOLD_URL%/}/api/v1/info/version" 2>/dev/null
}

alltalk_ready() {
    local response

    command -v curl >/dev/null 2>&1 || return 1
    response=$(curl --silent --fail \
        --connect-timeout 0.5 --max-time 1 \
        "http://127.0.0.1:${ALLTALK_PORT}/api/ready" 2>/dev/null) || return 1
    [[ "${response}" == Ready ]]
}

pgid_file() {
    REPLY="${RUNTIME_DIR}/$1.pgid"
}

record_managed_group() {
    local service=$1
    local pgid=$2

    pgid_file "${service}"
    print -r -- "${pgid}" >| "${REPLY}"
}

read_managed_group() {
    local service=$1
    local value

    pgid_file "${service}"
    [[ -r "${REPLY}" ]] || return 1
    IFS= read -r value < "${REPLY}" || return 1
    [[ "${value}" == <-> ]] || return 1
    (( value > 1 )) || return 1
    REPLY=${value}
}

pid_alive() {
    local pid=$1
    local state

    state=$(ps -o stat= -p "${pid}" 2>/dev/null) || return 1
    state=${state##[[:space:]]#}
    [[ -n "${state}" && "${state}" != Z* ]]
}

group_alive() {
    local pgid=$1
    local candidate state

    while read -r candidate state; do
        if [[ "${candidate}" == "${pgid}" && "${state}" != Z* ]]; then
            return 0
        fi
    done < <(ps -eo pgid=,stat=)
    return 1
}

process_group() {
    local pid=$1
    local pgid

    pgid=$(ps -o pgid= -p "${pid}" 2>/dev/null) || return 1
    pgid=${pgid//[[:space:]]/}
    [[ "${pgid}" == <-> ]] || return 1
    (( pgid > 1 )) || return 1
    REPLY=${pgid}
}

find_port_owner_pids() {
    local port=$1
    local output pid

    PORT_OWNER_PIDS=()
    output=$(fuser -n tcp "${port}" 2>/dev/null) || true
    for pid in ${(z)output}; do
        [[ "${pid}" == <-> ]] || continue
        (( pid == $$ )) && continue
        pid_alive "${pid}" || continue
        PORT_OWNER_PIDS+=("${pid}")
    done
    PORT_OWNER_PIDS=("${(@u)PORT_OWNER_PIDS}")
    (( ${#PORT_OWNER_PIDS} > 0 ))
}

signal_port_owners() {
    local signal_name=$1
    local pid pgid

    for pid in "${PORT_OWNER_PIDS[@]}"; do
        if process_group "${pid}" && (( REPLY == pid )); then
            pgid=${REPLY}
            kill -s "${signal_name}" -- "-${pgid}" 2>/dev/null || true
        else
            kill -s "${signal_name}" -- "${pid}" 2>/dev/null || true
        fi
    done
}

free_proxy_port() {
    local port=$1
    local deadline

    find_port_owner_pids "${port}" || return 0
    log "proxy port ${port} is owned by PID(s) ${(j:, :)PORT_OWNER_PIDS}; stopping them"
    signal_port_owners TERM

    deadline=$(( SECONDS + STOP_TIMEOUT ))
    while (( SECONDS < deadline )); do
        find_port_owner_pids "${port}" || {
            log "proxy port ${port} is available"
            return 0
        }
        sleep 0.2
    done

    log "proxy port ${port} is still occupied after ${STOP_TIMEOUT}s; sending SIGKILL"
    signal_port_owners KILL
    deadline=$(( SECONDS + 2 ))
    while (( SECONDS < deadline )); do
        find_port_owner_pids "${port}" || {
            log "proxy port ${port} is available"
            return 0
        }
        sleep 0.2
    done

    log "failed to release proxy port ${port}"
    return 1
}

ensure_proxy_ports_available() {
    if ! command -v fuser >/dev/null 2>&1; then
        log "fuser is required to identify processes that own proxy ports"
        return 1
    fi

    free_proxy_port "${PROXY_CHAT_PORT}" || return 1
    if [[ "${PROXY_IMAGE_PORT}" != "${PROXY_CHAT_PORT}" ]]; then
        free_proxy_port "${PROXY_IMAGE_PORT}" || return 1
    fi
}

launch_in_directory() {
    local service=$1
    local directory=$2
    local hide_output=$3
    local pid
    shift 3

    if (( hide_output && ! SHOW_CHILD_LOGS )); then
        setsid zsh -c 'cd -- "$1" || exit 1; shift; exec "$@"' \
            st-stack-child "${directory}" "$@" >/dev/null 2>&1 &
    else
        setsid zsh -c 'cd -- "$1" || exit 1; shift; exec "$@"' \
            st-stack-child "${directory}" "$@" &
    fi
    pid=$!
    record_managed_group "${service}" "${pid}"
}

ensure_service_started() {
    local service=$1

    if [[ "${service}" == cobol ]] && kobold_ready; then
        log "CobolCpp is already reachable at ${KOBOLD_URL}"
        return 0
    fi
    if [[ "${service}" == alltalk ]] && alltalk_ready; then
        log "AllTalk is already ready on port ${ALLTALK_PORT}"
        return 0
    fi
    if service_running "${service}"; then
        log "${service} is already running in its expected directory"
        return 0
    fi

    case "${service}" in
        cobol)
            log "starting CobolCpp in ${COBOL_DIR}"
            launch_in_directory cobol "${COBOL_DIR}" 1 "${COBOL_COMMAND}"
            ;;
        silly)
            log "starting SillyTavern in ${SILLY_DIR}"
            launch_in_directory silly "${SILLY_DIR}" 1 ./start.sh
            ;;
        alltalk)
            log "starting AllTalk in ${ALLTALK_DIR}"
            launch_in_directory alltalk "${ALLTALK_DIR}" 1 "${ALLTALK_COMMAND}"
            ;;
        proxy)
            log "starting proxy in ${SCRIPT_DIR}"
            launch_in_directory proxy "${SCRIPT_DIR}" 0 "${PROXY_COMMAND}" \
                --kobold-url "${KOBOLD_URL}" \
                --comfy-url "${COMFY_URL}" \
                --chat-port "${PROXY_CHAT_PORT}" \
                --image-port "${PROXY_IMAGE_PORT}"
            ;;
        *)
            log "internal error: unknown service ${service}"
            return 1
            ;;
    esac
}

wait_for_dependencies() {
    local deadline=$(( SECONDS + START_TIMEOUT ))
    local service
    local -a missing

    while (( SECONDS < deadline )); do
        missing=()
        kobold_ready || missing+=(cobol)
        alltalk_ready || missing+=(alltalk)
        for service in silly; do
            service_running "${service}" || missing+=("${service}")
        done
        if (( ${#missing} == 0 )); then
            log "CobolCpp, SillyTavern and AllTalk are running"
            return 0
        fi
        sleep 0.2
    done

    log "timed out waiting for dependencies: ${(j:, :)missing}"
    return 1
}

wait_for_proxy() {
    local deadline=$(( SECONDS + START_TIMEOUT ))

    while (( SECONDS < deadline )); do
        if service_running proxy; then
            log "proxy is running"
            return 0
        fi
        sleep 0.2
    done

    log "timed out waiting for proxy"
    return 1
}

collect_service_targets() {
    local service=$1
    local pid pgid service_port=""

    TARGET_PIDS=()
    TARGET_PGIDS=()

    if find_service_pids "${service}"; then
        for pid in "${MATCHED_PIDS[@]}"; do
            if process_group "${pid}" && (( REPLY == pid )); then
                TARGET_PGIDS+=("${REPLY}")
            else
                TARGET_PIDS+=("${pid}")
            fi
        done
    fi

    if read_managed_group "${service}"; then
        TARGET_PGIDS+=("${REPLY}")
    fi

    case "${service}" in
        cobol)
            service_port=${KOBOLD_PORT}
            ;;
        alltalk)
            service_port=${ALLTALK_PORT}
            ;;
    esac
    if [[ -n "${service_port}" ]] && find_port_owner_pids "${service_port}"; then
        for pid in "${PORT_OWNER_PIDS[@]}"; do
            if process_group "${pid}" && (( REPLY == pid )); then
                TARGET_PGIDS+=("${REPLY}")
            else
                TARGET_PIDS+=("${pid}")
            fi
        done
    fi

    TARGET_PIDS=("${(@u)TARGET_PIDS}")
    TARGET_PGIDS=("${(@u)TARGET_PGIDS}")
}

signal_targets() {
    local signal_name=$1
    local pid pgid

    for pgid in "${TARGET_PGIDS[@]}"; do
        kill -s "${signal_name}" -- "-${pgid}" 2>/dev/null || true
    done
    for pid in "${TARGET_PIDS[@]}"; do
        kill -s "${signal_name}" -- "${pid}" 2>/dev/null || true
    done
}

targets_running() {
    local service=$1
    local pid pgid

    service_running "${service}" && return 0
    for pgid in "${TARGET_PGIDS[@]}"; do
        group_alive "${pgid}" && return 0
    done
    for pid in "${TARGET_PIDS[@]}"; do
        pid_alive "${pid}" && return 0
    done
    return 1
}

clear_service_state() {
    local service=$1

    pgid_file "${service}"
    rm -f -- "${REPLY}"
}

stop_service() {
    local service=$1
    local deadline pid

    collect_service_targets "${service}"
    if (( ${#TARGET_PIDS} == 0 && ${#TARGET_PGIDS} == 0 )); then
        clear_service_state "${service}"
        return 0
    fi

    log "stopping ${service}"
    signal_targets TERM
    deadline=$(( SECONDS + STOP_TIMEOUT ))
    while (( SECONDS < deadline )); do
        if ! targets_running "${service}"; then
            clear_service_state "${service}"
            log "${service} stopped"
            return 0
        fi
        sleep 0.2
    done

    log "${service} did not stop within ${STOP_TIMEOUT}s; sending SIGKILL"
    if find_service_pids "${service}"; then
        for pid in "${MATCHED_PIDS[@]}"; do
            TARGET_PIDS+=("${pid}")
        done
        TARGET_PIDS=("${(@u)TARGET_PIDS}")
    fi
    signal_targets KILL
    sleep 0.2
    clear_service_state "${service}"

    if targets_running "${service}"; then
        log "failed to stop every ${service} process"
        return 1
    fi
    log "${service} stopped"
}

cleanup() {
    local result=0

    (( CLEANUP_STARTED )) && return 0
    CLEANUP_STARTED=1

    stop_service proxy || result=1
    stop_service alltalk || result=1
    stop_service silly || result=1
    stop_service cobol || result=1

    if (( OWNS_LOCK )); then
        rm -f -- "${SUPERVISOR_FILE}"
        rmdir -- "${LOCK_DIR}" 2>/dev/null || true
        OWNS_LOCK=0
    fi
    rmdir -- "${RUNTIME_DIR}" 2>/dev/null || true
    return ${result}
}

is_supervisor_pid() {
    local pid=$1
    local command

    [[ "${pid}" == <-> ]] || return 1
    (( pid > 1 )) || return 1
    pid_alive "${pid}" || return 1
    command=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null) || return 1
    [[ "${command}" == *"${SCRIPT_PATH:t}"* ]]
}

read_supervisor_pid() {
    local pid

    [[ -r "${SUPERVISOR_FILE}" ]] || return 1
    IFS= read -r pid < "${SUPERVISOR_FILE}" || return 1
    is_supervisor_pid "${pid}" || return 1
    REPLY=${pid}
}

acquire_supervisor_lock() {
    local existing_pid

    if mkdir -m 700 -- "${LOCK_DIR}" 2>/dev/null; then
        OWNS_LOCK=1
    else
        if read_supervisor_pid; then
            existing_pid=${REPLY}
            log "stack is already supervised by process ${existing_pid}"
            return 2
        fi
        rmdir -- "${LOCK_DIR}" 2>/dev/null || {
            log "could not remove stale supervisor lock"
            return 1
        }
        mkdir -m 700 -- "${LOCK_DIR}" || return 1
        OWNS_LOCK=1
    fi

    print -r -- "$$" >| "${SUPERVISOR_FILE}"
}

stop_stack() {
    local supervisor_pid
    local deadline
    local result=0

    if read_supervisor_pid; then
        supervisor_pid=${REPLY}
        log "requesting stack shutdown from supervisor ${supervisor_pid}"
        kill -s TERM -- "${supervisor_pid}" 2>/dev/null || true
        deadline=$(( SECONDS + (STOP_TIMEOUT * 4) + 5 ))
        while (( SECONDS < deadline )); do
            is_supervisor_pid "${supervisor_pid}" || return 0
            sleep 0.2
        done
        log "supervisor did not exit; stopping services directly"
    fi

    CLEANUP_STARTED=0
    stop_service proxy || result=1
    stop_service alltalk || result=1
    stop_service silly || result=1
    stop_service cobol || result=1
    rm -f -- "${SUPERVISOR_FILE}"
    rmdir -- "${LOCK_DIR}" 2>/dev/null || true
    rmdir -- "${RUNTIME_DIR}" 2>/dev/null || true

    if (( result == 0 )); then
        log "stack stopped"
    fi
    return ${result}
}

monitor_stack() {
    local service

    while true; do
        for service in proxy silly; do
            if ! service_running "${service}"; then
                log "${service} exited unexpectedly; stopping the stack"
                return 1
            fi
        done
        if ! service_running cobol && ! kobold_ready; then
            log "CobolCpp is no longer running or reachable; stopping the stack"
            return 1
        fi
        if ! service_running alltalk && ! alltalk_ready; then
            log "AllTalk is no longer running or ready; stopping the stack"
            return 1
        fi
        sleep 1
    done
}

TRAPINT() {
    log "received Ctrl-C; stopping the stack"
    exit 130
}

TRAPTERM() {
    log "received SIGTERM; stopping the stack"
    exit 143
}

TRAPHUP() {
    log "received SIGHUP; stopping the stack"
    exit 129
}

TRAPEXIT() {
    local exit_code=$?

    if (( OWNS_LOCK )); then
        cleanup || true
    fi
    return ${exit_code}
}

main() {
    local lock_status

    parse_args "$@" || return $?
    prepare_runtime_dir || return 1

    if (( STOP_ONLY )); then
        stop_stack
        return $?
    fi

    acquire_supervisor_lock
    lock_status=$?
    if (( lock_status == 2 )); then
        return 0
    elif (( lock_status != 0 )); then
        return ${lock_status}
    fi

    ensure_service_started cobol || return 1
    ensure_service_started silly || return 1
    ensure_service_started alltalk || return 1
    log "waiting for KoboldCpp readiness at ${KOBOLD_URL}"
    log "waiting for AllTalk readiness at http://127.0.0.1:${ALLTALK_PORT}/api/ready"
    wait_for_dependencies || return 1

    if service_running proxy; then
        ensure_service_started proxy || return 1
    else
        ensure_proxy_ports_available || return 1
        ensure_service_started proxy || return 1
    fi
    wait_for_proxy || return 1
    monitor_stack
}

main "$@"

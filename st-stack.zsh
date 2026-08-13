#!/usr/bin/env zsh

setopt EXTENDED_GLOB NO_NOMATCH PIPE_FAIL
set -u

umask 077

typeset -gr SCRIPT_PATH=${0:A}
typeset -gr SCRIPT_DIR=${SCRIPT_PATH:h}

typeset -gr SILLY_DIR="${HOME}/git/SillyTavern"
typeset -gr POCKETTTS_DIR="${HOME}/git/alltalk-pocket-tts-integraion"
typeset -gr ALLTALK_DIR="${HOME}/git/alltalk_tts"
typeset -gr ALLTALK_COMMAND="${HOME}/.dotfiles/config/dotconfig/scripts/start_all_talk.sh"
typeset -gr PROXY_COMMAND="./.venv/bin/st-vram-proxy"
typeset -gr PROXY_PATH="${SCRIPT_DIR}/.venv/bin/st-vram-proxy"
typeset -gr COMFY_URL="${ST_PROXY_COMFY_URL:-http://127.0.0.1:8188}"

typeset -gi LLM_COMMAND_EXPLICIT=${+ST_STACK_LLM_COMMAND}
typeset -g KOBOLD_EXECUTABLE="${ST_STACK_KOBOLD_EXECUTABLE:-./koboldcpp-linux-x64}"
typeset -g LLM_BACKEND="${ST_PROXY_LLM_BACKEND:-koboldcpp}"
typeset -g LLM_LABEL=""
typeset -g LLM_DEFAULT_URL=""
typeset -g LLM_DEFAULT_DIR=""
typeset -g LLM_DEFAULT_COMMAND=""
case "${LLM_BACKEND}" in
    koboldcpp)
        LLM_LABEL="KoboldCpp"
        LLM_DEFAULT_URL="http://127.0.0.1:5001"
        LLM_DEFAULT_DIR="${HOME}/git/cobolcpp"
        LLM_DEFAULT_COMMAND="${KOBOLD_EXECUTABLE}"
        ;;
    ollama)
        LLM_LABEL="Ollama"
        LLM_DEFAULT_URL="http://127.0.0.1:11434"
        LLM_DEFAULT_DIR="${HOME}"
        LLM_DEFAULT_COMMAND="ollama serve"
        ;;
    *)
        LLM_LABEL="${LLM_BACKEND}"
        ;;
esac
typeset -g LLM_URL="${ST_PROXY_LLM_URL:-${ST_PROXY_KOBOLD_URL:-${LLM_DEFAULT_URL}}}"
typeset -g LLM_DIR="${ST_STACK_LLM_DIR:-${LLM_DEFAULT_DIR}}"
typeset -g LLM_COMMAND_TEXT="${ST_STACK_LLM_COMMAND:-${LLM_DEFAULT_COMMAND}}"
typeset -ga LLM_COMMAND=(${(z)LLM_COMMAND_TEXT})
typeset -g LLM_COMMAND_NAME=""
if (( ${#LLM_COMMAND} )); then
    LLM_COMMAND_NAME=${LLM_COMMAND[1]:t:l}
fi
typeset -g KOBOLD_CONFIG_DIR="${ST_STACK_KOBOLD_CONFIG_DIR:-${LLM_DIR}/models}"
typeset -g KOBOLD_CONFIG_SETTING="${ST_STACK_KOBOLD_CONFIG:-}"
typeset -g KOBOLD_SELECTED_CONFIG=""
typeset -ga KOBOLD_CONFIG_FILES=()

typeset -gr RUNTIME_BASE="${XDG_RUNTIME_DIR:-/tmp}"
typeset -gr RUNTIME_DIR="${RUNTIME_BASE}/st-stack-${UID}"
typeset -gr LOCK_DIR="${RUNTIME_DIR}/supervisor.lock"
typeset -gr SUPERVISOR_FILE="${RUNTIME_DIR}/supervisor.pid"

typeset -gi SHOW_CHILD_LOGS=0
typeset -gi STOP_ONLY=0
typeset -gi OWNS_LOCK=0
typeset -gi CLEANUP_STARTED=0
typeset -gi SHUTDOWN_REQUESTED=0
typeset -gi RUNTIME_ACTIVE=0
typeset -g START_TIMEOUT="${ST_STACK_START_TIMEOUT:-60}"
typeset -g STOP_TIMEOUT="${ST_STACK_STOP_TIMEOUT:-10}"
typeset -g PROXY_CHAT_PORT="${ST_PROXY_CHAT_PORT:-5002}"
typeset -g PROXY_IMAGE_PORT="${ST_PROXY_IMAGE_PORT:-8189}"
typeset -g PROXY_IDLE_TIMEOUT="${ST_PROXY_IDLE_TIMEOUT:-60}"
typeset -g LLM_PORT=""
typeset -g POCKETTTS_PORT="${ST_STACK_POCKETTTS_PORT:-8008}"
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
    if [[ "${POCKETTTS_PORT}" != <-> ]] || (( POCKETTTS_PORT < 1 || POCKETTTS_PORT > 65535 )); then
        log "ST_STACK_POCKETTTS_PORT must be an integer between 1 and 65535"
        return 2
    fi
    if [[ -z "${LLM_BACKEND}" || -z "${LLM_URL}" || -z "${LLM_DIR}" || -z "${LLM_COMMAND_NAME}" ]]; then
        log "LLM backend, URL, directory and command must be configured"
        return 2
    fi
    if [[ "${LLM_URL}" =~ '^https?://[^/:]+:([0-9]+)(/.*)?$' ]]; then
        LLM_PORT=${match[1]}
    else
        log "ST_PROXY_LLM_URL must include http(s), a host and an explicit port"
        return 2
    fi
}

kobold_config_display_name() {
    local config=$1
    local relative=${config#${KOBOLD_CONFIG_DIR}/}

    REPLY=${relative%.kcpps}
}

discover_kobold_configs() {
    if [[ ! -d "${KOBOLD_CONFIG_DIR}" ]]; then
        log "KoboldCpp config directory does not exist: ${KOBOLD_CONFIG_DIR}"
        return 1
    fi
    if [[ ! -r "${KOBOLD_CONFIG_DIR}" ]]; then
        log "KoboldCpp config directory is not readable: ${KOBOLD_CONFIG_DIR}"
        return 1
    fi

    KOBOLD_CONFIG_DIR=${KOBOLD_CONFIG_DIR:A}
    KOBOLD_CONFIG_FILES=("${KOBOLD_CONFIG_DIR}"/**/*.kcpps(N.))
    if (( ${#KOBOLD_CONFIG_FILES} == 0 )); then
        log "no KoboldCpp .kcpps configs found under ${KOBOLD_CONFIG_DIR}"
        return 1
    fi
}

resolve_configured_kobold_config() {
    local candidate

    if [[ "${KOBOLD_CONFIG_SETTING}" == /* ]]; then
        candidate=${KOBOLD_CONFIG_SETTING}
    else
        candidate="${KOBOLD_CONFIG_DIR}/${KOBOLD_CONFIG_SETTING}"
    fi
    [[ "${candidate}" == *.kcpps ]] || candidate+=".kcpps"
    candidate=${candidate:A}

    if [[ "${candidate}" != "${KOBOLD_CONFIG_DIR}"/* ]]; then
        log "ST_STACK_KOBOLD_CONFIG must select a config under ${KOBOLD_CONFIG_DIR}"
        return 1
    fi
    if [[ ! -f "${candidate}" || ! -r "${candidate}" ]]; then
        log "configured KoboldCpp config is not a readable file: ${candidate}"
        return 1
    fi

    KOBOLD_SELECTED_CONFIG=${candidate}
}

prompt_for_kobold_config() {
    local choice
    local config
    local index=1

    log "available KoboldCpp configurations:"
    for config in "${KOBOLD_CONFIG_FILES[@]}"; do
        kobold_config_display_name "${config}"
        print -ru2 -- "  ${index}) ${REPLY}"
        (( index++ ))
    done

    while true; do
        print -nru2 -- "Select configuration [1-${#KOBOLD_CONFIG_FILES}]: "
        if ! IFS= read -r choice; then
            log "no KoboldCpp config selected; set ST_STACK_KOBOLD_CONFIG for non-interactive startup"
            return 1
        fi
        if [[ "${choice}" == <-> ]] &&
            (( choice >= 1 && choice <= ${#KOBOLD_CONFIG_FILES} )); then
            KOBOLD_SELECTED_CONFIG=${KOBOLD_CONFIG_FILES[choice]}
            return 0
        fi
        log "enter a number between 1 and ${#KOBOLD_CONFIG_FILES}"
    done
}

configure_kobold_command() {
    local executable_path

    (( LLM_COMMAND_EXPLICIT )) && return 0
    discover_kobold_configs || return 1

    if [[ -n "${KOBOLD_CONFIG_SETTING}" ]]; then
        resolve_configured_kobold_config || return 1
    else
        prompt_for_kobold_config || return 1
    fi

    if [[ "${KOBOLD_EXECUTABLE}" == */* ]]; then
        if [[ "${KOBOLD_EXECUTABLE}" == /* ]]; then
            executable_path=${KOBOLD_EXECUTABLE}
        else
            executable_path="${LLM_DIR}/${KOBOLD_EXECUTABLE}"
        fi
        if [[ ! -x "${executable_path}" ]]; then
            log "KoboldCpp executable is not executable: ${executable_path}"
            return 1
        fi
    elif ! command -v "${KOBOLD_EXECUTABLE}" >/dev/null 2>&1; then
        log "KoboldCpp executable was not found in PATH: ${KOBOLD_EXECUTABLE}"
        return 1
    fi

    LLM_COMMAND=("${KOBOLD_EXECUTABLE}" --config "${KOBOLD_SELECTED_CONFIG}")
    LLM_COMMAND_NAME=${KOBOLD_EXECUTABLE:t:l}
    kobold_config_display_name "${KOBOLD_SELECTED_CONFIG}"
    log "selected KoboldCpp config: ${REPLY}"
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
    PROCESS_COMMAND=$(
        { tr '\0' ' ' < "/proc/${pid}/cmdline"; } 2>/dev/null
    ) || return 1
    [[ -n "${PROCESS_COMMAND}" ]]
}

process_matches_service() {
    local service=$1
    local pid=$2
    local command_lower

    read_process_info "${pid}" || return 1
    command_lower=${PROCESS_COMMAND:l}

    case "${service}" in
        llm)
            [[ "${PROCESS_CWD}" == "${LLM_DIR}" ]] &&
                [[ "${command_lower}" == *"${LLM_COMMAND_NAME}"* ]]
            ;;
        silly)
            [[ "${PROCESS_CWD}" == "${SILLY_DIR}" ]] &&
                [[ "${command_lower}" == *start.sh* ||
                    ( "${command_lower}" == *python* && "${command_lower}" == *main.py* ) ||
                    ( "${command_lower}" == *node* && "${command_lower}" == *server.js* ) ]]
            ;;
        pockettts)
            [[ "${PROCESS_CWD}" == "${POCKETTTS_DIR}" ]] &&
                [[ "${command_lower}" == *pockettts-bridge* ]]
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

llm_ready() {
    [[ -x "${PROXY_PATH}" ]] || return 1
    "${PROXY_PATH}" \
        --check-backend \
        --backend-check-timeout 1 \
        --llm-backend "${LLM_BACKEND}" \
        --llm-url "${LLM_URL}" >/dev/null 2>&1
}

pockettts_ready() {
    command -v curl >/dev/null 2>&1 || return 1
    curl --silent --fail --output /dev/null \
        --connect-timeout 0.5 --max-time 1 \
        "http://127.0.0.1:${POCKETTTS_PORT}/health" 2>/dev/null
}

alltalk_ready() {
    local response

    command -v curl >/dev/null 2>&1 || return 1
    response=$(curl --silent --fail \
        --connect-timeout 0.5 --max-time 1 \
        "http://127.0.0.1:${ALLTALK_PORT}/api/ready" 2>/dev/null) || return 1
    [[ "${response}" == Ready ]]
}

typeset -g BROKER_STATUS_RESPONSE=""

broker_status() {
    BROKER_STATUS_RESPONSE=""

    command -v curl >/dev/null 2>&1 || return 1
    BROKER_STATUS_RESPONSE=$(curl --silent --fail \
        --connect-timeout 0.5 --max-time 1 \
        "http://127.0.0.1:${PROXY_CHAT_PORT}/broker/status" 2>/dev/null) || return 1
    [[ -n "${BROKER_STATUS_RESPONSE}" ]]
}

broker_status_is_healthy() {
    [[ "${BROKER_STATUS_RESPONSE}" == *'"healthy": true'* ||
        "${BROKER_STATUS_RESPONSE}" == *'"healthy":true'* ]]
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

    if [[ "${service}" == llm ]] && llm_ready; then
        log "${LLM_LABEL} is already reachable at ${LLM_URL}"
        return 0
    fi
    if [[ "${service}" == pockettts ]] && pockettts_ready; then
        log "PocketTTS bridge is already ready on port ${POCKETTTS_PORT}"
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
        llm)
            if [[ "${LLM_BACKEND}" == koboldcpp ]]; then
                configure_kobold_command || return 1
            fi
            log "starting ${LLM_LABEL} in ${LLM_DIR}"
            launch_in_directory llm "${LLM_DIR}" 1 "${LLM_COMMAND[@]}"
            ;;
        silly)
            log "starting SillyTavern in ${SILLY_DIR}"
            launch_in_directory silly "${SILLY_DIR}" 1 ./start.sh
            ;;
        pockettts)
            log "starting PocketTTS bridge in ${POCKETTTS_DIR}"
            launch_in_directory pockettts "${POCKETTTS_DIR}" 1 \
                uv run pockettts-bridge --config bridge.toml
            ;;
        alltalk)
            log "starting AllTalk in ${ALLTALK_DIR}"
            launch_in_directory alltalk "${ALLTALK_DIR}" 1 "${ALLTALK_COMMAND}"
            ;;
        proxy)
            log "starting proxy in ${SCRIPT_DIR}"
            launch_in_directory proxy "${SCRIPT_DIR}" 0 "${PROXY_COMMAND}" \
                --llm-backend "${LLM_BACKEND}" \
                --llm-url "${LLM_URL}" \
                --comfy-url "${COMFY_URL}" \
                --chat-port "${PROXY_CHAT_PORT}" \
                --image-port "${PROXY_IMAGE_PORT}" \
                --idle-timeout "${PROXY_IDLE_TIMEOUT}"
            ;;
        *)
            log "internal error: unknown service ${service}"
            return 1
            ;;
    esac
}

wait_for_pockettts() {
    local deadline=$(( SECONDS + START_TIMEOUT ))

    while (( SECONDS < deadline )); do
        if pockettts_ready; then
            log "PocketTTS bridge is ready"
            return 0
        fi
        sleep 0.2
    done

    log "timed out waiting for PocketTTS bridge on port ${POCKETTTS_PORT}"
    return 1
}

wait_for_dependencies() {
    local deadline=$(( SECONDS + START_TIMEOUT ))
    local service
    local -a missing

    while (( SECONDS < deadline )); do
        missing=()
        llm_ready || missing+=(llm)
        pockettts_ready || missing+=(pockettts)
        alltalk_ready || missing+=(alltalk)
        for service in silly; do
            service_running "${service}" || missing+=("${service}")
        done
        if (( ${#missing} == 0 )); then
            log "${LLM_LABEL}, SillyTavern, PocketTTS bridge and AllTalk are running"
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
        if service_running proxy && broker_status; then
            if broker_status_is_healthy; then
                log "proxy is running and healthy"
            else
                log "proxy is running in a degraded state; keeping it available"
            fi
            return 0
        fi
        sleep 0.2
    done

    log "timed out waiting for the proxy status endpoint"
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
        llm)
            service_port=${LLM_PORT}
            ;;
        pockettts)
            service_port=${POCKETTTS_PORT}
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
    stop_service pockettts || result=1
    stop_service silly || result=1
    stop_service llm || result=1

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
        deadline=$(( SECONDS + (STOP_TIMEOUT * 5) + 5 ))
        while (( SECONDS < deadline )); do
            is_supervisor_pid "${supervisor_pid}" || return 0
            sleep 0.2
        done
        log "supervisor did not exit; stopping services directly"
    fi

    CLEANUP_STARTED=0
    stop_service proxy || result=1
    stop_service alltalk || result=1
    stop_service pockettts || result=1
    stop_service silly || result=1
    stop_service llm || result=1
    rm -f -- "${SUPERVISOR_FILE}"
    rmdir -- "${LOCK_DIR}" 2>/dev/null || true
    rmdir -- "${RUNTIME_DIR}" 2>/dev/null || true

    if (( result == 0 )); then
        log "stack stopped"
    fi
    return ${result}
}

monitor_stack() {
    local current_proxy_state="healthy"
    local previous_proxy_state="healthy"
    local silly_available=1
    local pockettts_available=1
    local alltalk_available=1

    while true; do
        if ! service_running proxy; then
            current_proxy_state="exited"
        elif ! broker_status; then
            current_proxy_state="unresponsive"
        elif broker_status_is_healthy; then
            current_proxy_state="healthy"
        else
            current_proxy_state="degraded"
        fi

        if [[ "${current_proxy_state}" != "${previous_proxy_state}" ]]; then
            case "${current_proxy_state}" in
                healthy)
                    log "proxy recovered and reports healthy"
                    ;;
                degraded)
                    log "proxy reports an unhealthy or stalled coordinator; keeping it running"
                    ;;
                unresponsive)
                    log "proxy status endpoint is unavailable; leaving the proxy process running"
                    ;;
                exited)
                    log "proxy exited unexpectedly; keeping the remaining stack running"
                    ;;
            esac
            previous_proxy_state=${current_proxy_state}
        fi

        if service_running silly; then
            if (( ! silly_available )); then
                log "SillyTavern is running again"
                silly_available=1
            fi
        elif (( silly_available )); then
            log "SillyTavern is no longer running; keeping the proxy running"
            silly_available=0
        fi

        if ! service_running pockettts && ! pockettts_ready; then
            if (( pockettts_available )); then
                log "PocketTTS bridge is no longer running or ready; keeping the proxy running"
                pockettts_available=0
            fi
        elif (( ! pockettts_available )); then
            log "PocketTTS bridge is running or ready again"
            pockettts_available=1
        fi

        if ! service_running alltalk && ! alltalk_ready; then
            if (( alltalk_available )); then
                log "AllTalk is no longer running or ready; keeping the proxy running"
                alltalk_available=0
            fi
        elif (( ! alltalk_available )); then
            log "AllTalk is running or ready again"
            alltalk_available=1
        fi
        sleep 1
    done
}

TRAPINT() {
    SHUTDOWN_REQUESTED=1
    log "received Ctrl-C; stopping the stack"
    exit 130
}

TRAPTERM() {
    SHUTDOWN_REQUESTED=1
    log "received SIGTERM; stopping the stack"
    exit 143
}

TRAPHUP() {
    SHUTDOWN_REQUESTED=1
    log "received SIGHUP; stopping the stack"
    exit 129
}

TRAPEXIT() {
    local exit_code=$?

    if (( OWNS_LOCK && (SHUTDOWN_REQUESTED || ! RUNTIME_ACTIVE) )); then
        cleanup || true
    elif (( OWNS_LOCK )); then
        log "supervisor exited without an explicit shutdown; leaving services running"
        rm -f -- "${SUPERVISOR_FILE}"
        rmdir -- "${LOCK_DIR}" 2>/dev/null || true
        rmdir -- "${RUNTIME_DIR}" 2>/dev/null || true
        OWNS_LOCK=0
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

    ensure_service_started llm || return 1
    ensure_service_started silly || return 1
    ensure_service_started pockettts || return 1
    log "waiting for PocketTTS bridge readiness at http://127.0.0.1:${POCKETTTS_PORT}/health"
    wait_for_pockettts || return 1
    ensure_service_started alltalk || return 1
    log "waiting for ${LLM_LABEL} readiness at ${LLM_URL}"
    log "waiting for AllTalk readiness at http://127.0.0.1:${ALLTALK_PORT}/api/ready"
    wait_for_dependencies || return 1

    if service_running proxy; then
        ensure_service_started proxy || return 1
    else
        ensure_proxy_ports_available || return 1
        ensure_service_started proxy || return 1
    fi
    wait_for_proxy || return 1
    RUNTIME_ACTIVE=1
    monitor_stack
}

main "$@"

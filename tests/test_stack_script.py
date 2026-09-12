from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STACK_SCRIPT = REPO_ROOT / "st-stack.zsh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class FakeStack:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.repo = root / "st-proxy"
        self.outside = root / "outside"
        self.runtime = root / "runtime"
        self.events = root / "events.log"
        self.curl_log = root / "curl.log"
        self.bin_dir = root / "bin"

        self.llm_dir = self.home / "git" / "cobolcpp"
        self.silly_dir = self.home / "git" / "SillyTavern"
        self.pockettts_dir = self.home / "git" / "alltalk-pocket-tts-integraion"
        self.alltalk_dir = self.home / "git" / "alltalk_tts"
        self.alltalk_command = (
            self.home / ".dotfiles" / "config" / "dotconfig" / "scripts" / "start_all_talk.sh"
        )

        for directory in (self.repo, self.outside, self.runtime, self.bin_dir):
            directory.mkdir(parents=True)

        self.script = self.repo / "st-stack.zsh"
        shutil.copy2(STACK_SCRIPT, self.script)
        self.script.chmod(0o755)

        self._create_services()
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin_dir}:{self.env['PATH']}",
                "STACK_EVENT_LOG": str(self.events),
                "STACK_CURL_LOG": str(self.curl_log),
                "ST_PROXY_LLM_BACKEND": "koboldcpp",
                "ST_PROXY_LLM_URL": "http://127.0.0.1:5001",
                "ST_STACK_LLM_DIR": str(self.llm_dir),
                "ST_STACK_LLM_COMMAND": "./start_HQ.sh",
                "ST_STACK_ALLTALK_PORT": "7851",
                "ST_STACK_POCKETTTS_PORT": "8008",
                "ST_STACK_START_TIMEOUT": "3",
                "ST_STACK_STOP_TIMEOUT": "2",
                "XDG_RUNTIME_DIR": str(self.runtime),
            }
        )

    def _create_services(self) -> None:
        def shell_service(service: str) -> str:
            return f"""#!/bin/sh
set -eu
printf '%s|%s|%s\\n' "{service}" "$PWD" "$$" >> "$STACK_EVENT_LOG"
printf '%s-child-log\\n' "{service}"
trap 'exit 0' INT TERM HUP
while :; do sleep 1; done
"""

        _write_executable(self.llm_dir / "start_HQ.sh", shell_service("llm"))
        _write_executable(
            self.llm_dir / "koboldcpp-linux-x64", shell_service("llm")
        )

        _write_executable(self.silly_dir / "start.sh", shell_service("silly"))

        _write_executable(self.bin_dir / "uv", shell_service("pockettts"))
        self.pockettts_dir.mkdir(parents=True)

        _write_executable(self.alltalk_command, shell_service("alltalk"))
        self.alltalk_dir.mkdir(parents=True)

        fake_proxy = """#!/bin/sh
set -eu
check_backend=0
llm_url=
model_cache=
previous=
for argument in "$@"; do
    if [ "$argument" = "--check-backend" ]; then
        check_backend=1
    elif [ "$previous" = "--llm-url" ]; then
        llm_url=$argument
    elif [ "$previous" = "--write-kobold-model-cache" ]; then
        model_cache=$argument
    fi
    previous=$argument
done
if [ "$check_backend" -eq 1 ]; then
    if [ -n "$model_cache" ]; then
        printf '%s\\n' '{"object":"list","data":[{"id":"initial_model"}]}' > "$model_cache"
    fi
    printf '%s\\n' "$llm_url" >> "$STACK_CURL_LOG"
    ready_file=${FAKE_LLM_READY_FILE:-}
    if [ -n "$ready_file" ]; then
        [ -e "$ready_file" ]
        exit
    fi
    [ -f "$STACK_EVENT_LOG" ] || exit 1
    pid=$(awk -F '|' '$1 == "llm" { pid = $3 } END { print pid }' "$STACK_EVENT_LOG")
    [ -n "$pid" ] || exit 1
    state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$state" in
        ''|Z*) exit 1 ;;
    esac
    exit 0
fi
printf '%s|%s|%s\\n' "proxy" "$PWD" "$$" >> "$STACK_EVENT_LOG"
printf '%s-child-log\\n' "proxy"
trap 'exit 0' INT TERM HUP
while :; do sleep 1; done
"""
        _write_executable(self.repo / ".venv" / "bin" / "st-vram-proxy", fake_proxy)

        fake_fuser = """#!/bin/sh
set -eu
pid=${FAKE_PORT_OWNER_PID:-}
port=${FAKE_PORT_OWNER_PORT:-}
if [ -z "$pid" ] || [ "${3:-}" != "$port" ]; then
    exit 1
fi
state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
case "$state" in
    ''|Z*) exit 1 ;;
esac
printf '%s\\n' "$pid"
"""
        _write_executable(self.bin_dir / "fuser", fake_fuser)

        fake_curl = """#!/bin/sh
set -eu
url=
for argument in "$@"; do url=$argument; done
printf '%s\\n' "$url" >> "$STACK_CURL_LOG"

service_alive() {
    service=$1
    [ -f "$STACK_EVENT_LOG" ] || return 1
    pid=$(awk -F '|' -v service="$service" \
        '$1 == service { pid = $3 } END { print pid }' "$STACK_EVENT_LOG")
    [ -n "$pid" ] || return 1
    state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$state" in
        ''|Z*) return 1 ;;
    esac
}

case "$url" in
    */broker/status)
        service_alive proxy || exit 1
        unhealthy_file=${FAKE_BROKER_UNHEALTHY_FILE:-}
        if [ -n "$unhealthy_file" ] && [ -e "$unhealthy_file" ]; then
            printf 'broker-status:unhealthy\n' >> "$STACK_CURL_LOG"
            printf '{"healthy":false}'
        else
            printf 'broker-status:healthy\n' >> "$STACK_CURL_LOG"
            printf '{"healthy":true}'
        fi
        ;;
    */health)
        ready_file=${FAKE_POCKETTTS_READY_FILE:-}
        if [ -n "$ready_file" ]; then
            [ -e "$ready_file" ] || exit 1
        else
            service_alive pockettts || exit 1
        fi
        ;;
    */api/ready)
        ready_file=${FAKE_ALLTALK_READY_FILE:-}
        if [ -n "$ready_file" ]; then
            [ -e "$ready_file" ] || exit 1
        else
            service_alive alltalk || exit 1
        fi
        printf 'Ready'
        ;;
    *)
        ready_file=${FAKE_KOBOLD_READY_FILE:-}
        if [ -n "$ready_file" ]; then
            [ -e "$ready_file" ]
        else
            service_alive llm
        fi
        ;;
esac
"""
        _write_executable(self.bin_dir / "curl", fake_curl)

    def start(
        self, *args: str, stdin_data: str | None = None
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["zsh", str(self.script), *args],
            cwd=self.outside,
            env=self.env,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if stdin_data is not None:
            assert process.stdin is not None
            process.stdin.write(stdin_data)
            process.stdin.flush()
        return process

    def read_events(self) -> list[tuple[str, Path, int]]:
        if not self.events.exists():
            return []
        events = []
        for line in self.events.read_text(encoding="utf-8").splitlines():
            service, cwd, pid = line.split("|", maxsplit=2)
            events.append((service, Path(cwd), int(pid)))
        return events

    def wait_for_services(
        self, process: subprocess.Popen[str], expected: set[str], timeout: float = 8
    ) -> list[tuple[str, Path, int]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.read_events()
            if expected <= {event[0] for event in events}:
                return events
            if process.poll() is not None:
                output = process.communicate()[0]
                pytest.fail(f"stack exited before services started:\n{output}")
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {sorted(expected)}; events={self.read_events()}")


@pytest.fixture
def fake_stack(tmp_path: Path) -> FakeStack:
    return FakeStack(tmp_path)


@pytest.mark.parametrize("show_logs", [False, True])
def test_start_order_working_directories_and_logging(
    fake_stack: FakeStack, show_logs: bool
) -> None:
    process = fake_stack.start(*(("--log",) if show_logs else ()))
    events = fake_stack.wait_for_services(
        process, {"llm", "pockettts", "proxy"}
    )

    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")
    proxy_command = Path(f"/proc/{proxy_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    readiness_requests = fake_stack.curl_log.read_text(encoding="utf-8").splitlines()

    process.send_signal(signal.SIGINT)
    output = process.communicate(timeout=12)[0]

    assert process.returncode == 130
    first_event = {service: (index, cwd) for index, (service, cwd, _pid) in enumerate(events)}
    assert set(first_event) == {"llm", "pockettts", "proxy"}
    assert not any("/api/ready" in url for url in readiness_requests)
    assert first_event["llm"][1] == fake_stack.llm_dir
    assert first_event["pockettts"][1] == fake_stack.pockettts_dir
    assert first_event["proxy"][1] == fake_stack.repo
    dependency_indexes = [
        first_event[name][0] for name in ("llm", "pockettts")
    ]
    assert first_event["proxy"][0] > max(dependency_indexes)

    assert "--llm-backend koboldcpp" in proxy_command
    assert "--llm-url http://127.0.0.1:5001" in proxy_command
    assert "--comfy-url http://127.0.0.1:8188" in proxy_command
    assert "--chat-port 5002" in proxy_command
    assert "--image-port 8189" in proxy_command
    assert "--idle-timeout 60" in proxy_command
    assert "--restore-llm-on-idle" not in proxy_command

    assert "http://127.0.0.1:5001" in readiness_requests
    assert "http://127.0.0.1:8008/health" in readiness_requests

    assert "st-stack: starting KoboldCpp" in output
    assert "SillyTavern" not in output
    assert "st-stack: starting PocketTTS bridge" in output
    assert "AllTalk" not in output
    assert "proxy-child-log" in output
    for child_log in (
        "llm-child-log",
        "pockettts-child-log",
    ):
        assert (child_log in output) is show_logs


def test_koboldcpp_config_setting_accepts_relative_name_without_extension(
    fake_stack: FakeStack,
) -> None:
    config = (
        fake_stack.llm_dir
        / "models"
        / "roleplay"
        / "gemma4"
        / "primary config.kcpps"
    )
    config.parent.mkdir(parents=True)
    config.write_text("{}\n", encoding="utf-8")
    other = fake_stack.llm_dir / "models" / "other" / "deep" / "primary config.kcpps"
    other.parent.mkdir(parents=True)
    other.write_text('{"threads": 3}\n', encoding="utf-8")
    fake_stack.env.pop("ST_STACK_LLM_COMMAND")
    fake_stack.env["ST_STACK_KOBOLD_CONFIG"] = "roleplay/gemma4/primary config"

    supervisor = fake_stack.start()
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    llm_pid = next(pid for service, _cwd, pid in events if service == "llm")
    llm_command = (
        Path(f"/proc/{llm_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    )
    runtime = fake_stack.runtime / f"st-stack-{os.getuid()}"
    admin_dirs = list(runtime.glob("kobold-admin.*"))
    assert len(admin_dirs) == 1
    links = list(admin_dirs[0].glob("*.kcpps"))
    assert len(links) == 2 and all(link.is_symlink() for link in links)
    assert {link.resolve() for link in links} == {config, other}
    assert len({link.name for link in links}) == 2
    assert (runtime / "kobold-models.json").is_file()
    assert "--admin --routermode --admindir" in llm_command
    supervisor.send_signal(signal.SIGINT)
    output = supervisor.communicate(timeout=12)[0]

    assert supervisor.returncode == 130
    assert f"--config {config}" in llm_command
    assert "selected KoboldCpp config: roleplay/gemma4/primary config" in output
    assert "Select configuration" not in output
    assert not admin_dirs[0].exists()
    assert not (runtime / "kobold-models.json").exists()
    assert config.read_text() == "{}\n"
    assert other.read_text() == '{"threads": 3}\n'


def test_koboldcpp_prompts_with_sorted_names_without_idle_restore(
    fake_stack: FakeStack,
) -> None:
    first = fake_stack.llm_dir / "models" / "general" / "alpha.kcpps"
    selected = (
        fake_stack.llm_dir
        / "models"
        / "roleplay"
        / "gemma4"
        / "primary config.kcpps"
    )
    for config in (selected, first):
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{}\n", encoding="utf-8")
    fake_stack.env.pop("ST_STACK_LLM_COMMAND")
    fake_stack.env.pop("ST_STACK_KOBOLD_CONFIG", None)

    supervisor = fake_stack.start(stdin_data="0\nnot-a-number\n2\n")
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    llm_pid = next(pid for service, _cwd, pid in events if service == "llm")
    llm_command = (
        Path(f"/proc/{llm_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    )
    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")
    proxy_command = (
        Path(f"/proc/{proxy_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    )
    supervisor.send_signal(signal.SIGINT)
    output = supervisor.communicate(timeout=12)[0]

    assert supervisor.returncode == 130
    assert f"--config {selected}" in llm_command
    assert "  1) general/alpha" in output
    assert "  2) roleplay/gemma4/primary config" in output
    assert "Select configuration [1-2]:" in output
    assert output.count("enter a number between 1 and 2") == 2
    assert "Restore KoboldCpp" not in output
    assert "--restore-llm-on-idle" not in proxy_command
    assert "primary config.kcpps" not in output


def test_koboldcpp_auto_start_fails_when_models_have_no_configs(
    fake_stack: FakeStack,
) -> None:
    (fake_stack.llm_dir / "models").mkdir()
    fake_stack.env.pop("ST_STACK_LLM_COMMAND")
    fake_stack.env.pop("ST_STACK_KOBOLD_CONFIG", None)

    process = fake_stack.start()
    output = process.communicate(timeout=10)[0]

    assert process.returncode == 1
    assert "no KoboldCpp .kcpps configs found under" in output
    assert fake_stack.read_events() == []


def test_stop_flag_stops_the_supervised_stack(fake_stack: FakeStack) -> None:
    supervisor = fake_stack.start()
    fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )

    stopped = subprocess.run(
        ["zsh", str(fake_stack.script), "--stop"],
        cwd=fake_stack.outside,
        env=fake_stack.env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    supervisor_output = supervisor.communicate(timeout=12)[0]

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert supervisor.returncode == 143, supervisor_output
    assert "requesting stack shutdown" in stopped.stderr


def test_existing_dependencies_are_reused_and_stopped(fake_stack: FakeStack) -> None:
    external = [
        subprocess.Popen(
            [str(fake_stack.llm_dir / "start_HQ.sh")],
            cwd=fake_stack.llm_dir,
            env=fake_stack.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
        subprocess.Popen(
            [str(fake_stack.silly_dir / "start.sh")],
            cwd=fake_stack.silly_dir,
            env=fake_stack.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
        subprocess.Popen(
            ["uv", "run", "pockettts-bridge", "--config", "bridge.toml"],
            cwd=fake_stack.pockettts_dir,
            env=fake_stack.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
        subprocess.Popen(
            [str(fake_stack.alltalk_command)],
            cwd=fake_stack.alltalk_dir,
            env=fake_stack.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
    ]

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if {"llm", "silly", "pockettts", "alltalk"} <= {
                service for service, _cwd, _pid in fake_stack.read_events()
            }:
                break
            time.sleep(0.05)
        else:
            pytest.fail("external dependency mocks did not start")

        supervisor = fake_stack.start()
        fake_stack.wait_for_services(
            supervisor, {"llm", "pockettts", "proxy"}
        )
        supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

        assert supervisor.returncode == 130
        assert "KoboldCpp is already reachable" in output
        assert "silly is already running" not in output
        assert "PocketTTS bridge is already ready on port 8008" in output
        assert "AllTalk" not in output
        event_names = [service for service, _cwd, _pid in fake_stack.read_events()]
        assert event_names.count("llm") == 1
        assert event_names.count("silly") == 1
        assert event_names.count("pockettts") == 1
        assert event_names.count("alltalk") == 1
        for process in (external[0], external[2]):
            process.wait(timeout=5)
        for process in (external[1], external[3]):
            assert process.poll() is None
    finally:
        for process in external:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def test_dependency_timeout_prevents_proxy_start(fake_stack: FakeStack) -> None:
    fake_stack.env["FAKE_LLM_READY_FILE"] = str(fake_stack.root / "llm-not-ready")
    fake_stack.env["ST_STACK_START_TIMEOUT"] = "1"

    process = fake_stack.start()
    output = process.communicate(timeout=10)[0]

    assert process.returncode == 1
    assert "timed out waiting for dependencies: llm" in output
    assert "proxy" not in {service for service, _cwd, _pid in fake_stack.read_events()}


def test_proxy_port_owner_is_stopped_before_proxy_start(fake_stack: FakeStack) -> None:
    owner = subprocess.Popen(["sleep", "300"], start_new_session=True)
    fake_stack.env.update(
        {
            "FAKE_PORT_OWNER_PID": str(owner.pid),
            "FAKE_PORT_OWNER_PORT": "8189",
        }
    )

    try:
        supervisor = fake_stack.start()
        fake_stack.wait_for_services(
            supervisor, {"llm", "pockettts", "proxy"}
        )
        owner.wait(timeout=5)
        supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

        assert supervisor.returncode == 130
        assert owner.returncode == -signal.SIGTERM
        assert f"proxy port 8189 is owned by PID(s) {owner.pid}" in output
        assert "proxy port 8189 is available" in output
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=5)


def test_proxy_waits_for_pockettts_bridge_readiness(fake_stack: FakeStack) -> None:
    ready_file = fake_stack.root / "pockettts-ready"
    fake_stack.env["FAKE_POCKETTTS_READY_FILE"] = str(ready_file)
    supervisor = fake_stack.start()

    try:
        fake_stack.wait_for_services(supervisor, {"llm", "pockettts"})
        time.sleep(0.5)
        event_names = {service for service, _cwd, _pid in fake_stack.read_events()}
        assert "alltalk" not in event_names
        assert "proxy" not in event_names

        ready_file.touch()
        fake_stack.wait_for_services(
            supervisor, {"llm", "pockettts", "proxy"}
        )
        supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

        assert supervisor.returncode == 130
        assert "waiting for PocketTTS bridge readiness" in output
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=5)


def test_proxy_waits_for_llm_backend_readiness(fake_stack: FakeStack) -> None:
    ready_file = fake_stack.root / "llm-ready"
    fake_stack.env["FAKE_LLM_READY_FILE"] = str(ready_file)
    supervisor = fake_stack.start()

    try:
        fake_stack.wait_for_services(supervisor, {"llm", "pockettts"})
        time.sleep(0.5)
        assert "proxy" not in {
            service for service, _cwd, _pid in fake_stack.read_events()
        }

        ready_file.touch()
        fake_stack.wait_for_services(
            supervisor, {"llm", "pockettts", "proxy"}
        )
        supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

        assert supervisor.returncode == 130
        assert "waiting for KoboldCpp readiness" in output
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=5)


def test_ollama_profile_uses_generic_llm_configuration(fake_stack: FakeStack) -> None:
    ollama_dir = fake_stack.home / "ollama"
    ollama_command = ollama_dir / "start-ollama.sh"
    _write_executable(
        ollama_command,
        """#!/bin/sh
set -eu
printf '%s|%s|%s\\n' "llm" "$PWD" "$$" >> "$STACK_EVENT_LOG"
trap 'exit 0' INT TERM HUP
while :; do sleep 1; done
""",
    )
    fake_stack.env.update(
        {
            "ST_PROXY_LLM_BACKEND": "ollama",
            "ST_PROXY_LLM_URL": "http://127.0.0.1:11434",
            "ST_STACK_LLM_DIR": str(ollama_dir),
            "ST_STACK_LLM_COMMAND": "./start-ollama.sh",
        }
    )

    supervisor = fake_stack.start()
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")
    proxy_command = Path(f"/proc/{proxy_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    supervisor.send_signal(signal.SIGINT)
    output = supervisor.communicate(timeout=12)[0]

    assert supervisor.returncode == 130
    assert "--llm-backend ollama" in proxy_command
    assert "--llm-url http://127.0.0.1:11434" in proxy_command
    assert "starting Ollama" in output


def test_monitor_allows_adapter_to_stop_llm_process(fake_stack: FakeStack) -> None:
    supervisor = fake_stack.start()
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    llm_pid = next(pid for service, _cwd, pid in events if service == "llm")
    os.killpg(llm_pid, signal.SIGTERM)
    time.sleep(1.5)

    assert supervisor.poll() is None
    supervisor.send_signal(signal.SIGINT)
    output = supervisor.communicate(timeout=12)[0]
    assert supervisor.returncode == 130, output


def test_monitor_keeps_proxy_running_when_pockettts_exits(
    fake_stack: FakeStack,
) -> None:
    supervisor = fake_stack.start()
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    pockettts_pid = next(pid for service, _cwd, pid in events if service == "pockettts")
    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")

    try:
        os.killpg(pockettts_pid, signal.SIGTERM)
        time.sleep(1.5)
        assert supervisor.poll() is None
        os.kill(proxy_pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

    assert supervisor.returncode == 130, output
    assert "PocketTTS bridge is no longer running or ready; keeping the proxy running" in output


def test_monitor_keeps_proxy_running_when_broker_reports_backend_unavailable(
    fake_stack: FakeStack,
) -> None:
    unhealthy_file = fake_stack.root / "broker-unhealthy"
    fake_stack.env["FAKE_BROKER_UNHEALTHY_FILE"] = str(unhealthy_file)
    supervisor = fake_stack.start()
    events = fake_stack.wait_for_services(
        supervisor, {"llm", "pockettts", "proxy"}
    )
    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")

    def wait_for_status_marker(marker: str, previous_count: int = 0) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            entries = fake_stack.curl_log.read_text(encoding="utf-8").splitlines()
            if entries.count(marker) > previous_count:
                return
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {marker}")

    try:
        wait_for_status_marker("broker-status:healthy")
        unhealthy_file.touch()
        wait_for_status_marker("broker-status:unhealthy")
        assert supervisor.poll() is None
        os.kill(proxy_pid, 0)

        healthy_count = fake_stack.curl_log.read_text(encoding="utf-8").splitlines().count(
            "broker-status:healthy"
        )
        unhealthy_file.unlink()
        wait_for_status_marker("broker-status:healthy", healthy_count)
        observed_healthy_count = (
            fake_stack.curl_log.read_text(encoding="utf-8")
            .splitlines()
            .count("broker-status:healthy")
        )
        wait_for_status_marker("broker-status:healthy", observed_healthy_count)
        assert supervisor.poll() is None
        os.kill(proxy_pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

    assert supervisor.returncode == 130, output
    assert "proxy reports an unhealthy or stalled coordinator; keeping it running" in output
    assert "proxy recovered and reports healthy" in output


def test_proxy_does_not_require_alltalk_ready_response(fake_stack: FakeStack) -> None:
    ready_file = fake_stack.root / "alltalk-ready"
    fake_stack.env["FAKE_ALLTALK_READY_FILE"] = str(ready_file)
    supervisor = fake_stack.start()

    try:
        fake_stack.wait_for_services(
            supervisor, {"llm", "pockettts", "proxy"}
        )
        supervisor.send_signal(signal.SIGINT)
        output = supervisor.communicate(timeout=12)[0]

        assert supervisor.returncode == 130
        assert "AllTalk" not in output
        assert not ready_file.exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=5)


@pytest.mark.parametrize("interactive", [False, True])
def test_tabby_backend_selection_and_existing_start_flow(
    fake_stack: FakeStack, interactive: bool,
) -> None:
    tabby_dir = fake_stack.home / "git" / "tabby"
    _write_executable(tabby_dir / "start.sh", (fake_stack.llm_dir / "start_HQ.sh").read_text())
    for key in ("ST_PROXY_LLM_URL", "ST_STACK_LLM_DIR", "ST_STACK_LLM_COMMAND"):
        fake_stack.env.pop(key)
    fake_stack.env["ST_PROXY_KOBOLD_URL"] = "http://127.0.0.1:5999"
    fake_stack.env["ST_PROXY_KOBOLD_ROUTER_MODE"] = "true"
    if interactive:
        fake_stack.env.pop("ST_PROXY_LLM_BACKEND")
    else:
        fake_stack.env["ST_PROXY_LLM_BACKEND"] = "tabbyapi"
    process = fake_stack.start(stdin_data="2\n" if interactive else None)
    events = fake_stack.wait_for_services(process, {"llm", "pockettts", "proxy"})
    proxy_pid = next(pid for service, _cwd, pid in events if service == "proxy")
    command = Path(f"/proc/{proxy_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    environment = Path(f"/proc/{proxy_pid}/environ").read_bytes().split(b"\0")
    if interactive:
        stopped = subprocess.run(
            ["zsh", str(fake_stack.script), "--stop"],
            cwd=fake_stack.outside, env=fake_stack.env, capture_output=True,
            text=True, timeout=15, check=False,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "Select LLM backend" not in stopped.stderr
    else:
        process.send_signal(signal.SIGINT)
    output = process.communicate(timeout=12)[0]
    assert process.returncode == (143 if interactive else 130)
    assert "--llm-backend tabbyapi" in command
    assert "--llm-url http://127.0.0.1:5003" in command
    assert "--kobold-router-mode" not in command
    assert b"ST_PROXY_KOBOLD_ROUTER_MODE=false" in environment
    assert "starting TabbyAPI" in output
    assert "Select configuration" not in output
    assert ("Select LLM backend" in output) is interactive
    assert any(service == "llm" and cwd == tabby_dir for service, cwd, _pid in events)

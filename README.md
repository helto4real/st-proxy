# st-vram-proxy

`st-vram-proxy` is a loopback-only broker that gives one GPU exclusively to a
supported LLM backend or ComfyUI. SillyTavern talks to the broker on two ports,
while the real applications listen on different loopback ports behind it.

Supported LLM backends:

- KoboldCpp, controlled through Model Administration.
- Ollama, controlled through `/api/ps` and the `keep_alive` lifecycle API.

New to the project? Start with the illustrated
[user guide](docs/USER_GUIDE.md). The gateway and lease boundaries are described
in [Gateway and GPU lease architecture](docs/GATEWAY_AND_GPU_LEASES.md).

```text
SillyTavern chat  -> 127.0.0.1:5001 -> broker -> selected LLM backend
SillyTavern image -> 127.0.0.1:8188 -> broker -> ComfyUI   127.0.0.1:8189
Browser ComfyUI   -> 127.0.0.1:8188 -> broker -> ComfyUI HTTP + WebSocket
```

The image listener is a full ComfyUI gateway for HTTP and WebSocket traffic.
Browsing the interface, uploading inputs, and receiving live `/ws` updates do
not change GPU ownership. Chat and ComfyUI workflow submissions enter one FIFO
queue. The broker changes
GPU ownership only when the request at the head of that queue needs the other
backend. When switching to ComfyUI, it drains active chat responses (including
streams), asks the selected adapter to release its GPU resources, and waits for
the adapter to confirm release. Consecutive image jobs then run serially without
reloading the LLM between them.

When an LLM request reaches the head of the queue, the broker calls ComfyUI
`/free`, restores the LLM state captured at startup, verifies readiness, and
releases the chat request. If ComfyUI owns the GPU and the queue remains empty
for 60 seconds, the broker performs that same verified restore proactively so
the LLM is ready for the next chat. New work resets the idle timer. No request
can overtake a request for the other backend.

When the broker starts, it validates the selected LLM lifecycle API, calls
ComfyUI `/free`, and captures one ready LLM model before accepting chat
requests. This clears VRAM that a previously used ComfyUI instance may still
hold. If any startup check fails, chat remains fail-closed with HTTP 503.

For KoboldCpp, startup verifies Model Administration and the `unload_model` and
`initial_model` options. For Ollama, startup requires exactly one model in
`/api/ps`; this makes the restore target deterministic.

If ComfyUI cleanup or the final LLM readiness check fails, chat remains
fail-closed and gets HTTP 503. The latest error is visible at
`GET /broker/status` on either broker port.

The broker owns ComfyUI lifecycle routes such as `POST /free`. Unknown mutating
custom-node routes are rejected by default because they may perform GPU work
outside the workflow queue. Reviewed non-GPU routes in the Helto privacy,
utility, Director, Smart Prompt, and all-in-one image-generation packs are
method-scoped passthrough exceptions. Reviewed release/unload routes are
allowed only while ComfyUI owns the GPU. Compatibility passthrough is an
explicit opt-in for everything else.

## Requirements

- Python 3.11 or newer
- KoboldCpp with Admin mode enabled, or Ollama with exactly one loaded model
- ComfyUI API mode
- SillyTavern configured with separate chat and ComfyUI URLs

The examples keep every service on loopback. Do not expose the broker, an LLM
control API, or ComfyUI directly to an untrusted network.

## Install

### Linux / Arch Linux

```bash
cd /path/to/st-proxy
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Windows PowerShell

```powershell
cd C:\path\to\st-proxy
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## Start the applications

Start the backends first. Replace paths and normal model options with the ones
you already use.

### KoboldCpp

KoboldCpp must use a port other than the broker's chat port and must have Admin
mode enabled. `--admindir` is required by KoboldCpp for reload operations:

```bash
./koboldcpp-linux-x64 \
  --model /path/to/model.gguf \
  --host 127.0.0.1 \
  --port 5002 \
  --admin \
  --admindir /path/to/kobold-admin-files
```

If Admin mode is password protected, set the same secret only in the broker's
environment. Avoid placing it directly on the broker command line:

```bash
export ST_PROXY_KOBOLD_ADMIN_PASSWORD='replace-me'
```

On Windows, use the equivalent KoboldCpp GUI fields: port `5002`, host
`127.0.0.1`, Admin enabled, and an Admin/config directory. Keep the model you
want restored selected as the startup model.

### Ollama

Start Ollama and preload exactly one model:

```bash
ollama serve
ollama run gemma3 ""
```

The proxy records the model returned by `/api/ps`, unloads it with
`keep_alive: 0`, and restores it with `keep_alive: -1`.

### ComfyUI

Start ComfyUI on a different loopback port:

```bash
python main.py --listen 127.0.0.1 --port 8189
```

Windows portable example:

```powershell
.\python_embeded\python.exe ComfyUI\main.py --listen 127.0.0.1 --port 8189
```

Then start the broker:

```bash
st-vram-proxy \
  --llm-backend koboldcpp \
  --llm-url http://127.0.0.1:5002 \
  --comfy-url http://127.0.0.1:8189 \
  --chat-port 5001 \
  --image-port 8188
```

For Ollama, select the other adapter and origin:

```bash
st-vram-proxy \
  --llm-backend ollama \
  --llm-url http://127.0.0.1:11434 \
  --comfy-url http://127.0.0.1:8189
```

Check lifecycle control and model readiness without starting the listeners:

```bash
st-vram-proxy --check-backend --llm-backend ollama
```

The equivalent environment-only configuration is:

```bash
export ST_PROXY_LLM_BACKEND=koboldcpp
export ST_PROXY_LLM_URL=http://127.0.0.1:5002
export ST_PROXY_COMFY_URL=http://127.0.0.1:8189
export ST_PROXY_CHAT_PORT=5001
export ST_PROXY_IMAGE_PORT=8188
st-vram-proxy
```

## Configure SillyTavern

1. In SillyTavern's API Connections panel, select the API type matching your LLM
   backend and set its server URL to `http://127.0.0.1:5001`.
2. In the Image Generation extension, select ComfyUI and set its server URL to
   `http://127.0.0.1:8188`.
3. Keep the real LLM and ComfyUI on their upstream ports. SillyTavern should not
   point directly to either upstream.
4. Open the ComfyUI browser interface through the proxy image URL when you want
   all browser HTTP and WebSocket traffic to use the same gateway.

No SillyTavern, LLM-backend, or ComfyUI source changes are needed.

Check state without touching either backend directly:

```bash
curl http://127.0.0.1:5001/broker/status
```

Example idle response:

```json
{
  "state": "llm_ready",
  "gpu_owner": "llm",
  "active_chats": 0,
  "active_comfy_controls": 0,
  "waiting_chats": 0,
  "waiting_images": 0,
  "active_prompt_id": null,
  "last_error": null,
  "chat_available": true,
  "llm_backend": "koboldcpp",
  "idle_timeout": 60.0,
  "idle_restore_scheduled": false,
  "comfy_route_policy": "strict"
}
```

## Configuration

Every command-line setting has an `ST_PROXY_...` environment equivalent.

| CLI option | Environment variable | Default |
| --- | --- | --- |
| `--listen-host` | `ST_PROXY_LISTEN_HOST` | `127.0.0.1` |
| `--chat-port` | `ST_PROXY_CHAT_PORT` | `5001` |
| `--image-port` | `ST_PROXY_IMAGE_PORT` | `8188` |
| `--llm-backend` | `ST_PROXY_LLM_BACKEND` | `koboldcpp` |
| `--llm-url` | `ST_PROXY_LLM_URL` | backend-specific |
| `--comfy-url` | `ST_PROXY_COMFY_URL` | `http://127.0.0.1:8189` |
| `--kobold-admin-password` | `ST_PROXY_KOBOLD_ADMIN_PASSWORD` | unset |
| `--request-timeout` | `ST_PROXY_REQUEST_TIMEOUT` | `600` seconds |
| `--image-timeout` | `ST_PROXY_IMAGE_TIMEOUT` | `1800` seconds |
| `--chat-drain-timeout` | `ST_PROXY_CHAT_DRAIN_TIMEOUT` | `1800` seconds |
| `--unload-timeout` | `ST_PROXY_UNLOAD_TIMEOUT` | `180` seconds |
| `--reload-timeout` | `ST_PROXY_RELOAD_TIMEOUT` | `600` seconds |
| `--cleanup-timeout` | `ST_PROXY_CLEANUP_TIMEOUT` | `60` seconds |
| `--idle-timeout` | `ST_PROXY_IDLE_TIMEOUT` | `60` seconds |
| `--poll-interval` | `ST_PROXY_POLL_INTERVAL` | `0.5` seconds |
| `--allow-unknown-comfy-routes` | `ST_PROXY_ALLOW_UNKNOWN_COMFY_ROUTES` | disabled |
| `--check-backend` | — | disabled |
| `--backend-check-timeout` | `ST_PROXY_BACKEND_CHECK_TIMEOUT` | `2` seconds |
| `--log-level` | `ST_PROXY_LOG_LEVEL` | `INFO` |

`--kobold-url` and `ST_PROXY_KOBOLD_URL` remain compatibility aliases for the
generic LLM origin. Backend defaults are `http://127.0.0.1:5002` for KoboldCpp
and `http://127.0.0.1:11434` for Ollama.

Strict ComfyUI route policy coordinates workflow and cancellation routes,
reserves lifecycle routes for the broker, and rejects unclassified mutations.
Use `--allow-unknown-comfy-routes` only for a trusted custom extension whose
mutating routes you have reviewed. Those routes are passed through without GPU
coordination. Helto Director prompt-optimizer execution remains blocked in
strict mode because it can start GPU work outside the normal workflow queue.

## Adding another LLM backend

Provider mechanics live under `src/st_proxy/llm/`; FIFO policy and ComfyUI do
not depend on a concrete provider. A new adapter must:

1. Implement `validate_control`, `snapshot_ready`, `release_gpu`, and
   `acquire_gpu` from `LlmBackend`.
2. Return from release/acquire only after their postcondition has been checked.
3. Use sanitized errors and avoid logging model names, prompts, or secrets.
4. Add one `BackendSpec` in `llm/registry.py`.
5. Add adapter contract tests. Coordinator/FIFO tests use a fake backend and
   should not need changes.

Use GPU lifecycle semantics rather than assuming every provider literally
unloads and reloads a model. A future adapter may stop/start a managed process
while keeping the same coordinator contract.

Upstream URLs cannot contain embedded credentials. Request bodies, prompts,
generated data, authorization headers, and the Admin password are never logged.
The HTTP access log is disabled.

At the default `INFO` level, the broker logs request methods, paths, response
status codes and durations; GPU ownership and handoff state changes; ComfyUI
VRAM cleanup; LLM release/acquire readiness; and controlled failures. It
does not log query strings, request or response bodies, model names, prompts,
generated content, or authorization values. ComfyUI confirms that `/free`
completed but does not report an exact number of bytes freed.
High-frequency successful ComfyUI `GET /history` polling is logged only at
`DEBUG`; failures remain visible at `WARNING`.

The broker enforces logical GPU ownership through verified application
lifecycle APIs; it is not a kernel GPU access-control layer. For a hard
single-ingress boundary, keep backend ports on an internal container or network
namespace and publish only the two broker ports. A second loopback port prevents
accidental use but does not prevent another local process from connecting to it.

## Automated tests: isolated and safe

The automated suite does **not** discover or contact installed applications.
It starts synthetic KoboldCpp, Ollama, and ComfyUI fixtures on OS-assigned loopback ports,
starts the broker itself on OS-assigned ports, and uses a process-local endpoint
registry. Test mode refuses conventional ports, external hosts, and any upstream
not explicitly registered by that test process.

Each test uses a disposable temporary working directory. The suite does not
read application directories or user content and requires no GPU or models.

```bash
python -m pip install -e '.[dev]'
python -B -m pytest
ruff check .
```

Do not override test fixtures with real URLs. `ST_PROXY_TEST_MODE` is
intentionally rejected by the CLI; the isolation registry exists only inside
the test harness.

## Optional manual integration test (user-run only)

This is never run by the automated suite. It contacts the real services you
started above and will unload/reload real models:

1. Confirm the configured LLM backend and ComfyUI are on their upstream ports.
2. Start the broker and confirm `/broker/status` says `llm_ready`.
3. Start a SillyTavern chat and let it finish.
4. Request one image from SillyTavern.
5. Watch `/broker/status`; it should progress through drain, unload, and image,
   then remain at `comfy_ready` with `gpu_owner` set to `comfy`.
6. Start the next SillyTavern chat. The status should progress through cleanup
   and reload, return to `llm_ready`, and complete without reconnecting.

Only perform this procedure when you explicitly intend to contact and control
those real local processes.

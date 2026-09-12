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
`/free`, restores the previously observed LLM state, verifies readiness, and
releases the chat request. ComfyUI keeps the GPU until an active LLM request
needs it; idle timers, workflow completion/failure, background health checks,
startup, and shutdown never restore KoboldCpp. No request can overtake a request
for the other backend.

KoboldCpp `GET /api/v1/model` and `GET /v1/models` are passive metadata. They
never take a chat lease or trigger lifecycle work. While ComfyUI owns the GPU,
the proxy returns a safe inactive response without contacting KoboldCpp.
In Router mode, `/v1/models` instead returns the cached available profiles.

KoboldCpp startup is passive: the broker opens its listeners without contacting
KoboldCpp or changing GPU ownership. The first active chat or workflow validates
Model Administration and discovers the current model state. Use
`--check-backend` for an explicit readiness check. Ollama retains its startup
snapshot requirement so its restore target remains deterministic.

If ComfyUI cleanup or the final LLM readiness check fails, chat remains
fail-closed and gets HTTP 503. The latest error is visible at
`GET /broker/status` on either broker port. After a runtime ownership failure,
the broker stays online but does no background recovery. A later active LLM
request may make one new coordinated attempt; it never treats a failed cleanup
as released VRAM.

Control traffic, chat streams, normal ComfyUI HTTP traffic, and ComfyUI
WebSockets use independent connection pools. Pool acquisition and upstream
read-idle waits are bounded, WebSockets use heartbeats, and abandoned workflow
requests are removed before submission. Workflow request bodies and queued
workflow memory are bounded independently of large streamed uploads.

The broker owns ComfyUI lifecycle routes such as `POST /free`, coordinates
workflow and control routes, and transparently forwards every other ComfyUI
HTTP/WebSocket route by default. This avoids per-node allowlist maintenance.
Reviewed release/unload routes are allowed only while ComfyUI owns the GPU, and
known routes that start GPU work outside the workflow queue remain blocked.

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

### KoboldCpp native Router mode

Enable this mode to let KoboldCpp select a `.kcpps` profile from the request's
`model` field. The broker still owns the GPU handoff to/from ComfyUI. It admits
one model-dependent request at a time and holds the reservation throughout
loading, generation and the complete upstream response, including disconnected
streams. It does not restore `initial_model` before forwarding a selected model.
Missing/null/empty text-request model fields use `initial_model`.

Start KoboldCpp with `--admin --routermode --admindir /path/to/profiles` in
addition to the normal startup configuration. Then export a passive model cache
and enable the broker mode (adjust origins and listener ports to your setup):

```bash
st-vram-proxy --llm-url http://127.0.0.1:5002 --kobold-router-mode \
  --check-backend --write-kobold-model-cache /tmp/kobold-models.json
st-vram-proxy --llm-url http://127.0.0.1:5002 --kobold-router-mode \
  --kobold-model-cache /tmp/kobold-models.json
```

Select the exact profile ID returned by the broker's `/v1/models` in an
OpenAI-compatible client. The list includes `.kcpps` profiles and `initial_model`,
not unload commands or claims that every profile is loaded. Without an initial
cache, only `initial_model` is listed until discovery can run under an idle LLM
reservation. Lists remain cached during ComfyUI work and active LLM requests;
GET discovery never loads models. Generic read-only metadata and abort also
bypass GPU acquisition. Abort is admitted while a model-dependent request is
active; it does not queue behind that request.

Text completions, chat completions, Kobold generation/streaming and token counts
are supported; `/api/latest/generate` and `/api/extra/tokenize` are normalized to
the native router's wake endpoints. Clients must send the same `model` when
counting tokens and generating. Other explicit model IDs retain KoboldCpp's
native behavior, including its handling of unknown IDs. Context/capability
metadata describes the currently loaded model, not every listed profile.

The status `llm_reserved` means KoboldCpp has exclusive permission to use the
GPU, not that a particular model is already loaded. A subsequent ComfyUI job
requires a confirmed `unload_model`, including after a failed router request.
Runtime failures keep the broker alive and never start background reloads.
Direct `/api/admin/reload_config` and `/noscript` calls through the broker are
blocked in this mode to prevent bypassing coordinated generation/lifecycle work.

The router transport supplies `Content-Length` and buffers only an admitted
request, capped by `--max-chat-body-bytes` (32 MiB by default). Queueing clients
are not eagerly read into full request buffers. Router loading has its own
backend timeout; the broker's `request_timeout` also bounds upstream read-idle.

The standard `st-stack.zsh` KoboldCpp launch enables Router mode automatically,
retains the selected startup profile and exposes all recursively discovered
profiles through temporary flat symlinks. Profile IDs use the original basename
plus a stable relative-path hash, avoiding collisions between subdirectories.
The script exports the initial cache during readiness. Original profiles and
their relative-path working directory are unchanged. Restart the stack to
refresh profile links; runtime files are removed only after services stop.

Set `ST_PROXY_KOBOLD_ROUTER_MODE=false` for the old single-profile stack mode.
An explicit `ST_STACK_LLM_COMMAND` retains legacy behavior unless Router mode is
explicitly enabled; its command must then enable native Router mode itself.
An already running non-router backend must be restarted with the required flags.
Use regular Router mode for this text-profile integration; autoswap/multimodal
configuration management and direct backend clients are outside this contract.

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
  "responding": true,
  "ready": true,
  "state": "awaiting_request",
  "state_age_seconds": 4.2,
  "state_stalled": false,
  "healthy": true,
  "dispatcher_alive": true,
  "recovering": false,
  "recovery_attempts": 0,
  "recovery_retry_seconds": null,
  "gpu_owner": null,
  "active_chats": 0,
  "active_llm_metadata": 0,
  "active_comfy_controls": 0,
  "waiting_chats": 0,
  "waiting_images": 0,
  "queued_workflow_bytes": 0,
  "active_prompt_id": null,
  "last_error": null,
  "chat_available": true,
  "llm_backend": "koboldcpp",
  "idle_timeout": 60.0,
  "idle_restore_enabled": false,
  "idle_restore_scheduled": false,
  "comfy_route_policy": "transparent"
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
| `--kobold-router-mode` | `ST_PROXY_KOBOLD_ROUTER_MODE` | disabled (standard stack launch enables it) |
| `--kobold-model-cache` | `ST_PROXY_KOBOLD_MODEL_CACHE` | unset |
| `--max-chat-body-bytes` | `ST_PROXY_MAX_CHAT_BODY_BYTES` | `33554432` |
| `--write-kobold-model-cache` | — | explicit export with `--check-backend` |
| `--connect-timeout` | `ST_PROXY_CONNECT_TIMEOUT` | `30` seconds |
| `--request-timeout` | `ST_PROXY_REQUEST_TIMEOUT` | `3900` seconds read-idle |
| `--image-timeout` | `ST_PROXY_IMAGE_TIMEOUT` | `1800` seconds |
| `--chat-drain-timeout` | `ST_PROXY_CHAT_DRAIN_TIMEOUT` | `3900` seconds |
| `--unload-timeout` | `ST_PROXY_UNLOAD_TIMEOUT` | `180` seconds |
| `--reload-timeout` | `ST_PROXY_RELOAD_TIMEOUT` | `600` seconds |
| `--cleanup-timeout` | `ST_PROXY_CLEANUP_TIMEOUT` | `60` seconds |
| `--idle-timeout` | `ST_PROXY_IDLE_TIMEOUT` | `60` seconds |
| `--restore-llm-on-idle` | `ST_PROXY_RESTORE_LLM_ON_IDLE` | deprecated no-op |
| `--poll-interval` | `ST_PROXY_POLL_INTERVAL` | `0.5` seconds |
| `--comfy-poll-failure-limit` | `ST_PROXY_COMFY_POLL_FAILURE_LIMIT` | `6` |
| `--max-workflow-body-bytes` | `ST_PROXY_MAX_WORKFLOW_BODY_BYTES` | `67108864` |
| `--max-queued-images` | `ST_PROXY_MAX_QUEUED_IMAGES` | `32` |
| `--max-queued-workflow-bytes` | `ST_PROXY_MAX_QUEUED_WORKFLOW_BYTES` | `268435456` |
| `--strict-comfy-routes` | `ST_PROXY_STRICT_COMFY_ROUTES` | disabled |
| `--check-backend` | — | disabled |
| `--backend-check-timeout` | `ST_PROXY_BACKEND_CHECK_TIMEOUT` | `2` seconds |
| `--log-level` | `ST_PROXY_LOG_LEVEL` | `INFO` |

`--kobold-url` and `ST_PROXY_KOBOLD_URL` remain compatibility aliases for the
generic LLM origin. Backend defaults are `http://127.0.0.1:5002` for KoboldCpp
and `http://127.0.0.1:11434` for Ollama.

The default transparent ComfyUI route policy forwards unclassified extension
routes without GPU coordination. Use `--strict-comfy-routes` only when you want
the historical reviewed-route allowlist. Workflow and cancellation routes are
still coordinated, lifecycle routes remain broker-owned, and Helto Director
prompt-optimizer execution remains blocked in every mode because it can start
GPU work outside the normal workflow queue. The old
`--allow-unknown-comfy-routes` option remains a compatibility alias for the
default behavior.

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

### TabbyAPI / ExLlamaV3

`st-stack.zsh` first offers KoboldCpp or TabbyAPI. Set
`ST_PROXY_LLM_BACKEND=tabbyapi` to skip the menu. TabbyAPI uses the existing
`~/git/tabby/start.sh` and its native configuration, at `http://127.0.0.1:5003`.
The existing `ST_STACK_LLM_DIR`, `ST_STACK_LLM_COMMAND`, and `ST_PROXY_LLM_URL`
overrides still apply. No authentication is added; this adapter expects the
local TabbyAPI instance with authentication disabled.

For a server starting without a loaded model, set `ST_PROXY_TABBY_MODEL` to the
native model name. `ST_PROXY_TABBY_MAX_SEQ_LEN` defaults to `32768`. A loaded
model is observed and saved before handoff. Load settings absent from the API,
including CPU offload, must already persist through TabbyAPI's native
`model.use_as_default` or model-local configuration. See the
[TabbyAPI setup and limitations](docs/USER_GUIDE.md#tabbyapi--exllamav3).

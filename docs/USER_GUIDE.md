# st-vram-proxy user guide

`st-vram-proxy` lets SillyTavern use a supported LLM backend for chat and
ComfyUI for image generation on a single GPU. It queues requests in arrival
order and ensures that only one backend owns GPU VRAM at a time. KoboldCpp and
Ollama are currently supported.

This guide covers installation, first-time setup, everyday operation, status
monitoring, and troubleshooting.

![Architecture overview showing SillyTavern routed through st-vram-proxy to KoboldCpp and ComfyUI, with one GPU owner at a time](images/architecture-overview.png)

## Who this guide is for

Use this guide if:

- SillyTavern, a supported LLM backend, and ComfyUI run on the same computer.
- The LLM and ComfyUI models do not comfortably fit in VRAM
  together.
- You want image and chat requests to wait safely instead of competing for
  memory.

The broker does not install or configure models for you. The LLM backend and
ComfyUI must already work independently before you add the proxy.

## What the proxy does

SillyTavern connects to two proxy ports:

| Workload | SillyTavern connects to | Proxy forwards to |
| --- | --- | --- |
| Chat | `http://127.0.0.1:5001` | Selected LLM backend |
| Images | `http://127.0.0.1:8188` | ComfyUI at `http://127.0.0.1:8189` |

The image URL is also the browser URL for proxied ComfyUI access. The gateway
forwards the interface, uploads, previews, API responses, and live WebSocket
updates without changing GPU ownership. Only a workflow submission requests a
ComfyUI GPU lease.

The proxy starts with the selected LLM owning the GPU. When an image reaches the front
of the queue, it:

1. Stops starting newer chat requests.
2. Waits for active chats and streams to finish.
3. Asks the LLM adapter to release GPU resources and verifies the result.
4. Sends the image workflow to ComfyUI.
5. Leaves ComfyUI ready for any consecutive image requests.

When a chat reaches the front of the queue, it:

1. Waits for the active image to finish.
2. Asks ComfyUI to unload models and free memory.
3. Restores the LLM state captured at startup.
4. Verifies that the original model is ready.
5. Starts the queued chat.

If no request is waiting after an image, ComfyUI remains loaded for up to 60
seconds so consecutive image requests can reuse it. New work resets that idle
timer. Once the full idle period passes with no active or queued jobs, the proxy
frees ComfyUI and restores the startup LLM state so the next chat can begin
without waiting for a handoff.

![FIFO queue showing two consecutive images using one ComfyUI ownership period before switching once to KoboldCpp](images/lazy-fifo-flow.png)

### FIFO examples

| Request order | Result |
| --- | --- |
| Image 1 → Image 2 → Chat 1 | One switch to ComfyUI, both images run, then one switch back to the LLM |
| Image 1 → Chat 1 → Image 2 | Chat 1 runs between the images; Image 2 cannot overtake it |
| Chat 1 → Chat 2 → Image 1 | Both chats may run concurrently; the image waits until both finish |
| Image 1 → no new request | ComfyUI stays ready for 60 seconds, then the proxy restores the LLM |

## Requirements

- Python 3.11 or newer
- KoboldCpp with Model Administration and an Admin/config directory, or Ollama
  with exactly one loaded model
- ComfyUI running in API-compatible mode
- SillyTavern with an API type matching the selected LLM and ComfyUI image generation
- All services listening on the local computer

Keep the proxy and both backend Admin/API ports on loopback
(`127.0.0.1` or `localhost`). Do not expose them directly to an untrusted
network.

## Before installation

Confirm that the backend ports are free and distinct:

| Component | Default role | Default port |
| --- | --- | --- |
| st-vram-proxy chat listener | SillyTavern chat destination | `5001` |
| KoboldCpp | Default real chat backend | `5002` |
| Ollama | Alternative real chat backend | `11434` |
| st-vram-proxy image listener | SillyTavern image destination | `8188` |
| ComfyUI | Real image backend | `8189` |

If you choose different ports, update the backend commands, proxy command, and
SillyTavern settings together.

## Install st-vram-proxy

### Linux

From the repository directory:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

After installation, confirm that the command is available:

```bash
st-vram-proxy --help
```

If you open a new terminal later, reactivate the environment:

```bash
source .venv/bin/activate
```

### Windows PowerShell

From the repository directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Confirm the installation:

```powershell
st-vram-proxy --help
```

If PowerShell blocks activation scripts, either adjust the execution policy for
your user account or run the executable directly:

```powershell
.\.venv\Scripts\st-vram-proxy.exe --help
```

## Configure and start the backends

Start the real backends before starting the proxy.

### 1. Configure KoboldCpp

KoboldCpp must:

- Listen on a port different from the proxy chat port.
- Have Model Administration enabled.
- Have a valid Admin/config directory.
- Expose the `unload_model` and `initial_model` Admin options.
- Start with the model you want restored after image generation.

Linux example:

```bash
./koboldcpp-linux-x64 \
  --model /path/to/model.gguf \
  --host 127.0.0.1 \
  --port 5002 \
  --admin \
  --admindir /path/to/kobold-admin-files
```

On Windows, configure the equivalent fields in the KoboldCpp GUI:

- Host: `127.0.0.1`
- Port: `5002`
- Admin: enabled
- Admin/config directory: a writable directory
- Startup model: the model that should return after ComfyUI finishes

If KoboldCpp Admin is password protected, put the password in the proxy
environment rather than on the command line.

Linux:

```bash
export ST_PROXY_KOBOLD_ADMIN_PASSWORD='replace-me'
```

Windows PowerShell:

```powershell
$env:ST_PROXY_KOBOLD_ADMIN_PASSWORD = 'replace-me'
```

### 2. Alternatively, configure Ollama

Start Ollama and preload exactly one model:

```bash
ollama serve
ollama run gemma3 ""
```

Verify `ollama ps` shows exactly one model. The proxy uses Ollama's lifecycle
API to unload that model before ComfyUI work and reload it indefinitely when
chat returns.

### 3. Start ComfyUI

Linux example:

```bash
python main.py --listen 127.0.0.1 --port 8189
```

Windows portable example:

```powershell
.\python_embeded\python.exe ComfyUI\main.py --listen 127.0.0.1 --port 8189
```

Confirm that ComfyUI opens normally and that your intended SillyTavern workflow
can generate an image before introducing the proxy.

## Start the proxy

With the virtual environment active:

```bash
st-vram-proxy \
  --llm-backend koboldcpp \
  --llm-url http://127.0.0.1:5002 \
  --comfy-url http://127.0.0.1:8189 \
  --chat-port 5001 \
  --image-port 8188
```

Windows PowerShell uses the same options:

```powershell
st-vram-proxy `
  --llm-backend koboldcpp `
  --llm-url http://127.0.0.1:5002 `
  --comfy-url http://127.0.0.1:8189 `
  --chat-port 5001 `
  --image-port 8188
```

At startup, the proxy:

1. Verifies the selected LLM lifecycle API.
2. Calls ComfyUI `/free` to clear leftover image models.
3. Captures and confirms the startup LLM model.
4. Opens the two proxy listener ports.

A healthy startup ends with a log similar to:

```text
broker ready: chat=http://127.0.0.1:5001 image=http://127.0.0.1:8188 state=llm_ready
```

Leave this terminal running. Press `Ctrl+C` to stop the proxy cleanly. If
ComfyUI owns the GPU at shutdown, the proxy attempts to free ComfyUI and restore
the selected LLM before exiting.

## Configure SillyTavern

### Chat connection

1. Open SillyTavern's API Connections panel.
2. Select the API type matching the configured backend.
3. Set the server URL to `http://127.0.0.1:5001`.
4. Connect normally.

### Image connection

1. Open the Image Generation extension settings.
2. Select ComfyUI.
3. Set the ComfyUI server URL to `http://127.0.0.1:8188`.
4. Select or configure the workflow you normally use.

SillyTavern should point to the proxy ports, not directly to the real LLM or
ComfyUI ports.

## Verify the setup

### 1. Check broker status

Linux:

```bash
curl http://127.0.0.1:5001/broker/status
```

Windows PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:5001/broker/status
```

Immediately after startup, expect:

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
  "comfy_route_policy": "strict"
}
```

The status endpoint works on either proxy port.

### 2. Test chat

Send a short SillyTavern chat message. It should complete normally while the
status remains `llm_ready`.

### 3. Test image generation

Request one image. During the handoff, the state progresses through:

```text
draining_llm → unloading_llm → image_active → comfy_ready
```

After the image finishes, `gpu_owner` remains `comfy`. This is intentional.

### 4. Test the return to chat

Send another chat message. The state progresses through:

```text
cleaning_comfy → reloading_llm → llm_ready
```

The chat starts after the original LLM model is verified.

## Understand the status endpoint

### Status fields

| Field | Meaning |
| --- | --- |
| `state` | Current handoff or processing stage |
| `gpu_owner` | `llm`, `comfy`, or `null` while ownership is being changed or is unknown |
| `active_chats` | Chat requests currently using the selected LLM |
| `active_comfy_controls` | Cancellation/control requests currently using the ComfyUI control plane |
| `waiting_chats` | Chat requests still waiting in the FIFO queue |
| `waiting_images` | Image requests still waiting in the FIFO queue |
| `active_prompt_id` | ComfyUI prompt currently being monitored, or `null` |
| `last_error` | Most recent controlled error or warning |
| `chat_available` | Whether the broker can accept chat requests; `true` does not mean the LLM is already loaded |
| `llm_backend` | Selected lifecycle adapter, such as `koboldcpp` or `ollama` |
| `idle_timeout` | Configured ComfyUI idle period in seconds |
| `idle_restore_scheduled` | Whether the broker is currently counting down to an idle LLM restore |
| `comfy_route_policy` | `strict` by default, or `compatible` when unknown mutations are explicitly allowed |

`waiting_chats` and `waiting_images` count queued work. The request currently
being activated or processed is not included in those counters.

### States

| State | Meaning |
| --- | --- |
| `initializing` | Coordinator is starting |
| `cleaning_comfy` | Proxy is asking ComfyUI to free models and memory |
| `verifying_llm` | Proxy is checking the startup LLM model |
| `llm_ready` | The selected LLM owns the GPU and chat can start |
| `draining_llm` | Newer work is queued while active chats finish |
| `unloading_llm` | The LLM adapter is releasing GPU resources |
| `comfy_ready` | ComfyUI owns the GPU and is idle between image jobs |
| `image_active` | A ComfyUI prompt is running |
| `reloading_llm` | The startup LLM state is being restored |
| `error` | GPU ownership or backend readiness could not be verified |
| `shutting_down` | Proxy is stopping and restoring a safe state |

## Everyday operation

- Start the selected LLM backend and ComfyUI before the proxy.
- Keep the proxy running while SillyTavern is in use.
- A chat waiting behind images is normal.
- An image waiting for a streaming chat is normal.
- Consecutive images reuse the ComfyUI ownership period.
- After 60 seconds without active or queued work, ComfyUI is freed and the
  selected LLM is restored and verified.
- Consecutive chats can run concurrently until an image reaches the front of
  the queue.
- Stop the proxy with `Ctrl+C` so it can restore the LLM safely.
- Use `/broker/status` before restarting backends during a handoff.

## Configuration reference

Every command-line option has an `ST_PROXY_...` environment equivalent.
Command-line values override only their corresponding defaults supplied by the
environment.

| Command-line option | Environment variable | Default | Purpose |
| --- | --- | --- | --- |
| `--listen-host` | `ST_PROXY_LISTEN_HOST` | `127.0.0.1` | Address used by both proxy listeners |
| `--chat-port` | `ST_PROXY_CHAT_PORT` | `5001` | SillyTavern chat destination |
| `--image-port` | `ST_PROXY_IMAGE_PORT` | `8188` | SillyTavern image destination |
| `--llm-backend` | `ST_PROXY_LLM_BACKEND` | `koboldcpp` | LLM lifecycle adapter |
| `--llm-url` | `ST_PROXY_LLM_URL` | backend-specific | Real LLM origin |
| `--comfy-url` | `ST_PROXY_COMFY_URL` | `http://127.0.0.1:8189` | Real ComfyUI origin |
| `--kobold-admin-password` | `ST_PROXY_KOBOLD_ADMIN_PASSWORD` | unset | KoboldCpp Admin bearer password |
| `--request-timeout` | `ST_PROXY_REQUEST_TIMEOUT` | `600` seconds | General upstream request timeout |
| `--image-timeout` | `ST_PROXY_IMAGE_TIMEOUT` | `1800` seconds | Maximum monitored image-job duration |
| `--chat-drain-timeout` | `ST_PROXY_CHAT_DRAIN_TIMEOUT` | `1800` seconds | Maximum wait for active chats to finish |
| `--unload-timeout` | `ST_PROXY_UNLOAD_TIMEOUT` | `180` seconds | Maximum LLM release/verification time |
| `--reload-timeout` | `ST_PROXY_RELOAD_TIMEOUT` | `600` seconds | Maximum LLM restore/verification time |
| `--cleanup-timeout` | `ST_PROXY_CLEANUP_TIMEOUT` | `60` seconds | Maximum ComfyUI cleanup time |
| `--idle-timeout` | `ST_PROXY_IDLE_TIMEOUT` | `60` seconds | ComfyUI idle period before proactively restoring the LLM |
| `--poll-interval` | `ST_PROXY_POLL_INTERVAL` | `0.5` seconds | Backend state polling interval |
| `--allow-unknown-comfy-routes` | `ST_PROXY_ALLOW_UNKNOWN_COMFY_ROUTES` | disabled | Pass unclassified trusted custom-node mutations through without coordination |
| `--check-backend` | — | disabled | Check LLM control and readiness, then exit |
| `--backend-check-timeout` | `ST_PROXY_BACKEND_CHECK_TIMEOUT` | `2` seconds | Readiness-command timeout |
| `--log-level` | `ST_PROXY_LOG_LEVEL` | `INFO` | Python logging level |

KoboldCpp defaults to `http://127.0.0.1:5002`; Ollama defaults to
`http://127.0.0.1:11434`. `--kobold-url` and `ST_PROXY_KOBOLD_URL` remain
compatibility aliases for the generic LLM origin.

Example environment-only configuration on Linux:

```bash
export ST_PROXY_LLM_BACKEND=koboldcpp
export ST_PROXY_LLM_URL=http://127.0.0.1:5002
export ST_PROXY_COMFY_URL=http://127.0.0.1:8189
export ST_PROXY_CHAT_PORT=5001
export ST_PROXY_IMAGE_PORT=8188
st-vram-proxy
```

## Troubleshooting

Start with two pieces of information:

```bash
curl http://127.0.0.1:5001/broker/status
```

Then read the proxy terminal around the most recent state transition. Logs do
not include prompts, generated content, request bodies, model names,
authorization values, or the Admin password.

### A chat or image appears to be waiting

Check:

- `state`
- `active_chats`
- `waiting_chats`
- `waiting_images`
- `active_prompt_id`

Waiting is expected when an earlier request owns the GPU. FIFO ordering means a
newer request never jumps ahead merely because its backend is already loaded.

If `active_chats` remains nonzero, a long-running or disconnected stream may
still be draining upstream. The proxy deliberately waits for upstream
generation to finish before asking the LLM adapter to release the GPU.

### Startup reports Model Administration errors

Typical messages mention:

- `model administration check failed`
- `admin options check failed`
- required options missing
- Admin request rejected

Verify:

1. KoboldCpp Admin mode is enabled.
2. `--admindir` or the GUI Admin/config directory is valid.
3. The Admin password matches `ST_PROXY_KOBOLD_ADMIN_PASSWORD`.
4. KoboldCpp exposes both `unload_model` and `initial_model`.
5. You restarted KoboldCpp after changing its Admin configuration.

This section applies only to the `koboldcpp` adapter.

### Ollama startup reports a ready-state snapshot error

The `ollama` adapter requires exactly one model in `/api/ps`. Run:

```bash
ollama ps
```

Load the intended model if none is present, or stop extra models before
restarting the proxy. The proxy deliberately refuses to guess which of several
models should be restored.

### Status remains `error`

The proxy fails closed when it cannot verify GPU ownership. Read `last_error`,
fix the backend problem, then restart the proxy. Do not bypass the proxy and
send work directly to both backends while ownership is uncertain.

### KoboldCpp unload times out

Symptoms include `confirmation of unloaded failed: timed out`.

Verify that:

- The Admin operation succeeds in KoboldCpp.
- `/api/v1/model` reports an inactive state after unload.
- KoboldCpp is responsive on the configured upstream port.
- The configured `--unload-timeout` is long enough for your system.

### KoboldCpp reload times out

Symptoms include `confirmation of loaded failed: timed out`.

The proxy requires the model after reload to match the model it observed at
startup. Check that:

- `initial_model` points to the intended startup model.
- The model file is still available.
- KoboldCpp has enough memory after ComfyUI cleanup.
- The configured `--reload-timeout` is long enough.

### ComfyUI image job times out

The proxy asks ComfyUI to interrupt the job, records the error, frees ComfyUI,
and restores the selected LLM.

Check the ComfyUI console for workflow or node errors. Increase
`--image-timeout` only if the workflow is healthy but legitimately takes
longer.

### ComfyUI cleanup fails

The proxy records the cleanup failure and still attempts to restore the LLM.
Check ComfyUI's `/free` support and console output. If the LLM cannot reload,
the broker enters `error` and returns HTTP 503 for queued chat.

### HTTP 502 versus HTTP 503

| Response | Meaning |
| --- | --- |
| HTTP 502 | A normal proxied request to the selected backend failed |
| HTTP 503 | The broker rejected work because shutdown, initialization, or GPU ownership was not safely resolved |

### Port already in use

Choose four distinct ports or stop the process using the conflicting port.
Remember that SillyTavern uses the proxy ports while the proxy uses the backend
ports.

On Linux, inspect a port with:

```bash
ss -ltnp | grep ':5001'
```

On Windows PowerShell:

```powershell
Get-NetTCPConnection -LocalPort 5001
```

### SillyTavern bypasses the proxy

Recheck both SillyTavern URLs:

- Chat must use the proxy chat port.
- Images must use the proxy image port.

Do not configure SillyTavern with the real LLM or ComfyUI ports.

### A custom-node web action gets HTTP 403

Strict mode rejects mutating routes it cannot classify because such a route may
run GPU work outside `/prompt`. Prefer adding and reviewing an explicit route
classification. For a trusted extension that requires broad compatibility, set
`ST_PROXY_ALLOW_UNKNOWN_COMFY_ROUTES=true`; this weakens the GPU-ownership
boundary for those routes.

Current versions explicitly pass the reviewed `helto-privacy` keystore routes
and Helto Director timeline encryption/decryption routes without taking a GPU
lease. Other mutating routes under those namespaces remain blocked by default.

## Security and privacy

- Loopback is the safe default.
- Do not expose an LLM lifecycle or Admin API publicly.
- Do not expose ComfyUI or the proxy directly to an untrusted network.
- Store the Admin password in `ST_PROXY_KOBOLD_ADMIN_PASSWORD`.
- Avoid putting passwords on command lines where shell history or process
  listings may reveal them.
- Upstream URLs containing embedded credentials are rejected.
- Request bodies, prompts, generated content, model names, and authorization
  headers are not logged.
- For a hard single-ingress boundary, place the real backends on an internal
  container/network-namespace network and expose only the broker listeners.
- The broker coordinates VRAM lifecycle APIs; it does not revoke CUDA device
  access at the operating-system level.

## Optional local stack supervisor

The repository includes `st-stack.zsh`, a Linux/Zsh supervisor tailored to the
repository owner's local multi-service setup. It can supervise a configured
LLM, SillyTavern, PocketTTS, AllTalk, and the proxy. It does not start ComfyUI.

This script is not a portable default installation:

- Its non-LLM application directories remain constants near the top of the
  script and must match your computer.
- Configure the LLM with `ST_PROXY_LLM_BACKEND`, `ST_PROXY_LLM_URL`, and
  `ST_STACK_LLM_DIR`.
- When using KoboldCpp without `ST_STACK_LLM_COMMAND`, the supervisor scans
  `ST_STACK_KOBOLD_CONFIG_DIR` recursively for `.kcpps` files. The config
  directory defaults to `models` beneath `ST_STACK_LLM_DIR`.
- Its default port layout differs from the standalone examples in this guide:
  its KoboldCpp profile expects the real LLM and ComfyUI origins on `5001` and `8188`, and
  exposes the proxy on `5002` and `8189`.
- It requires Zsh and Linux process-management tools.

After adapting it to your environment:

```bash
./st-stack.zsh
```

The interactive KoboldCpp list displays paths relative to `models` and omits
the `.kcpps` extension. To select a config without a prompt, set its relative
path with or without the extension:

```bash
export ST_STACK_KOBOLD_CONFIG='roleplay/gemma4/role-play-no-thinking-goetia-26b'
./st-stack.zsh
```

`ST_STACK_KOBOLD_EXECUTABLE` changes the KoboldCpp executable and defaults to
`./koboldcpp-linux-x64`. Setting `ST_STACK_LLM_COMMAND` preserves the generic
custom-command behavior and takes precedence over automatic KoboldCpp config
selection.

Show child-process output:

```bash
./st-stack.zsh --log
```

Stop the supervised stack:

```bash
./st-stack.zsh --stop
```

Do not use the supervisor until you have reviewed its configured directories,
commands, ports, and shutdown behavior.

## Quick reference

Start order:

```text
1. Selected LLM backend
2. ComfyUI
3. st-vram-proxy
4. SillyTavern connection
```

Default URLs:

```text
SillyTavern chat:   http://127.0.0.1:5001
Default LLM backend: http://127.0.0.1:5002
SillyTavern images: http://127.0.0.1:8188
ComfyUI backend:    http://127.0.0.1:8189
Broker status:      http://127.0.0.1:5001/broker/status
```

Healthy idle states:

```text
llm_ready   — the selected LLM owns the GPU
comfy_ready — ComfyUI owns the GPU during the configured idle grace period
```

Safe stop:

```text
Press Ctrl+C in the proxy terminal.
```

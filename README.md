# st-vram-proxy

`st-vram-proxy` is a loopback-only broker that gives one GPU exclusively to
KoboldCpp or ComfyUI. SillyTavern talks to the broker on two ports, while the
real applications listen on different loopback ports behind it.

New to the project? Start with the illustrated
[user guide](docs/USER_GUIDE.md).

```text
SillyTavern chat  -> 127.0.0.1:5001 -> broker -> KoboldCpp 127.0.0.1:5002
SillyTavern image -> 127.0.0.1:8188 -> broker -> ComfyUI   127.0.0.1:8189
```

Chat and ComfyUI `POST /prompt` requests enter one FIFO queue. The broker changes
GPU ownership only when the request at the head of that queue needs the other
backend. When switching to ComfyUI, it drains active chat responses (including
streams), unloads KoboldCpp, and confirms its model endpoint reports an inactive
model. Consecutive image jobs then run serially without reloading KoboldCpp
between them.

When an LLM request reaches the head of the queue, the broker calls ComfyUI
`/free`, reloads KoboldCpp's startup model, verifies that the same model seen at
startup is ready, and releases the chat request. If the queue becomes empty, the
current backend keeps the GPU: ComfyUI remains ready after an image until an LLM
request actually needs the GPU, and KoboldCpp remains ready until an image does.
No request can overtake a request for the other backend.

When the broker starts, it first calls ComfyUI `/free` and confirms that
KoboldCpp has a loaded model before accepting chat requests. This clears VRAM
that a previously used ComfyUI instance may still hold. If either startup check
fails, chat remains fail-closed with HTTP 503.

Before changing GPU ownership, startup also verifies that KoboldCpp reports
Model Administration enabled and exposes the `unload_model` and `initial_model`
admin options. A missing required directory or incorrect Admin password produces
an actionable console error and keeps the broker fail-closed.

If the final KoboldCpp readiness check fails, chat remains fail-closed and gets
HTTP 503. This prevents an accidental LLM request while GPU ownership is
unknown. The latest error is visible at `GET /broker/status` on either broker
port.

## Requirements

- Python 3.11 or newer
- KoboldCpp with Admin mode enabled
- ComfyUI API mode
- SillyTavern configured with separate KoboldCpp and ComfyUI URLs

The examples keep every service on loopback. Do not expose the broker,
KoboldCpp Admin API, or ComfyUI directly to an untrusted network.

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
  --kobold-url http://127.0.0.1:5002 \
  --comfy-url http://127.0.0.1:8189 \
  --chat-port 5001 \
  --image-port 8188
```

The equivalent environment-only configuration is:

```bash
export ST_PROXY_KOBOLD_URL=http://127.0.0.1:5002
export ST_PROXY_COMFY_URL=http://127.0.0.1:8189
export ST_PROXY_CHAT_PORT=5001
export ST_PROXY_IMAGE_PORT=8188
st-vram-proxy
```

## Configure SillyTavern

1. In SillyTavern's API Connections panel, select KoboldCpp/KoboldAI and set its
   server URL to `http://127.0.0.1:5001`. Connect normally.
2. In the Image Generation extension, select ComfyUI and set its server URL to
   `http://127.0.0.1:8188`.
3. Keep KoboldCpp itself on `5002` and ComfyUI itself on `8189`. SillyTavern
   should not point directly to either upstream port.

No SillyTavern, KoboldCpp, or ComfyUI source changes are needed.

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
  "waiting_chats": 0,
  "waiting_images": 0,
  "active_prompt_id": null,
  "last_error": null,
  "chat_available": true
}
```

## Configuration

Every command-line setting has an `ST_PROXY_...` environment equivalent.

| CLI option | Environment variable | Default |
| --- | --- | --- |
| `--listen-host` | `ST_PROXY_LISTEN_HOST` | `127.0.0.1` |
| `--chat-port` | `ST_PROXY_CHAT_PORT` | `5001` |
| `--image-port` | `ST_PROXY_IMAGE_PORT` | `8188` |
| `--kobold-url` | `ST_PROXY_KOBOLD_URL` | `http://127.0.0.1:5002` |
| `--comfy-url` | `ST_PROXY_COMFY_URL` | `http://127.0.0.1:8189` |
| `--kobold-admin-password` | `ST_PROXY_KOBOLD_ADMIN_PASSWORD` | unset |
| `--request-timeout` | `ST_PROXY_REQUEST_TIMEOUT` | `600` seconds |
| `--image-timeout` | `ST_PROXY_IMAGE_TIMEOUT` | `1800` seconds |
| `--chat-drain-timeout` | `ST_PROXY_CHAT_DRAIN_TIMEOUT` | `1800` seconds |
| `--unload-timeout` | `ST_PROXY_UNLOAD_TIMEOUT` | `180` seconds |
| `--reload-timeout` | `ST_PROXY_RELOAD_TIMEOUT` | `600` seconds |
| `--cleanup-timeout` | `ST_PROXY_CLEANUP_TIMEOUT` | `60` seconds |
| `--poll-interval` | `ST_PROXY_POLL_INTERVAL` | `0.5` seconds |
| `--log-level` | `ST_PROXY_LOG_LEVEL` | `INFO` |

Upstream URLs cannot contain embedded credentials. Request bodies, prompts,
generated data, authorization headers, and the Admin password are never logged.
The HTTP access log is disabled.

At the default `INFO` level, the broker logs request methods, paths, response
status codes and durations; GPU ownership and handoff state changes; ComfyUI
VRAM cleanup; KoboldCpp unload/reload readiness; and controlled failures. It
does not log query strings, request or response bodies, model names, prompts,
generated content, or authorization values. ComfyUI confirms that `/free`
completed but does not report an exact number of bytes freed.
High-frequency successful ComfyUI `GET /history` polling is logged only at
`DEBUG`; failures remain visible at `WARNING`.

## Automated tests: isolated and safe

The automated suite does **not** discover or contact installed applications.
It starts synthetic KoboldCpp and ComfyUI fixtures on OS-assigned loopback ports,
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

1. Confirm KoboldCpp is on `127.0.0.1:5002` and ComfyUI is on
   `127.0.0.1:8189`.
2. Start the broker and confirm `/broker/status` says `llm_ready`.
3. Start a SillyTavern chat and let it finish.
4. Request one image from SillyTavern.
5. Watch `/broker/status`; it should progress through drain, unload, and image,
   then remain at `comfy_ready` with `gpu_owner` set to `comfy`.
6. Start the next SillyTavern chat. The status should progress through cleanup
   and reload, return to `llm_ready`, and complete without reconnecting.

Only perform this procedure when you explicitly intend to contact and control
those real local processes.

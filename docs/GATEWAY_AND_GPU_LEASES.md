# Gateway and GPU lease architecture

`st-vram-proxy` separates the ComfyUI data plane from GPU ownership policy.
The image listener stays available as the one browser/API origin while the
coordinator decides whether the selected LLM or ComfyUI may hold model VRAM.

## Single ingress

Clients use only the broker listeners:

```text
chat client       -> broker chat listener  -> private LLM origin
ComfyUI browser   -> broker image listener -> private ComfyUI origin
workflow client   -> broker image listener -> FIFO coordinator -> ComfyUI
```

The image listener proxies normal HTTP responses, uploads, redirects, and the
bidirectional ComfyUI `/ws` connection. UI traffic does not itself acquire a GPU
lease. The proxy rewrites upstream `Origin`, `Referer`, and absolute `Location`
values so the private backend origin does not become the browser's navigation
target.

For an enforceable ingress boundary, run the proxy, ComfyUI, and the LLM on an
internal container network or in a dedicated network namespace. Publish only
the broker ports. Binding every service to a different loopback port is useful
protection against mistakes, but it does not stop another local process from
calling a backend directly.

## ComfyUI route policy

The gateway classifies each request before forwarding it:

| Class | Examples | Policy |
| --- | --- | --- |
| Read/UI | static assets, `GET`, `HEAD`, `OPTIONS`, `/ws` | Transparent passthrough; no GPU handoff |
| Workflow | `POST /prompt`, `POST /api/prompt` | Shared FIFO and full ComfyUI GPU lease |
| Control | `/interrupt`, `/queue`, their `/api` aliases, job cancellation, reviewed release/unload routes | Allowed only while ComfyUI owns the GPU; holds a short control lease |
| Lifecycle | `/free`, `/api/free` | Broker-internal; external calls get HTTP 403 |
| Known UI mutation | uploads, settings, userdata, history, reviewed custom-node operations | Transparent passthrough |
| Unknown mutation | new or updated custom-extension route | Transparent passthrough by default; no GPU handoff |
| Known uncoordinated GPU route | Director prompt-optimizer execution | Always HTTP 403 |

The transparent default keeps new and updated custom-node services working
without changing a central allowlist. `--strict-comfy-routes` changes the
unknown-mutation row to HTTP 403 and uses the historical reviewed-route list.
The old `--allow-unknown-comfy-routes` option remains a compatibility alias for
transparent mode.

Strict mode still recognizes reviewed non-GPU mutations from `helto-privacy`,
`comfyui-utils`, `comfyui-helto-director`, `comfyui-helto-smartprompt`, and
`comfyui-all-on-one-image-generation-node`. They are allowlisted by HTTP method
and exact path or narrow path pattern. This includes privacy, settings,
library, media-browser, selector, queue-manager, prompt-library, folder, and
metadata operations. This list no longer affects normal transparent operation.

Reviewed model-release and unload operations use the control class and are
accepted only while ComfyUI owns the GPU. Director prompt-optimizer execution
at `/helto_director/prompt_optimizer/optimize` and `/optimize/start` remains
blocked in every mode: it can start GPU work outside ComfyUI's normal
`/prompt` and history lifecycle, so it needs a separate coordinated lease
design before it can be enabled safely.

Transparent passthrough is not an ownership guarantee for an unknown extension
that starts CUDA work through its own endpoint. Such an endpoint must be added
to the small GPU-sensitive block/control set or given a dedicated coordinator
contract. The tradeoff is deliberate: routine web and storage services require
no central maintenance, while known GPU entry points remain explicit.

For compatibility with ComfyUI backends that expose only the legacy route
names, the gateway translates the frontend aliases `/api/free`,
`/api/interrupt`, `/api/prompt`, `/api/queue`, `/api/settings`,
`/api/userdata`, and `/api/users` to their unprefixed upstream equivalents.
Other `/api/...` routes are preserved because endpoints such as `/api/jobs`
and `/api/assets` are genuine API routes.

The transport preserves the browser's raw percent-encoded path. This is
required for nested userdata names: ComfyUI sends a path such as
`workflows/example.json` as the single route segment
`workflows%2Fexample.json`.

## Transport and memory isolation

Lifecycle/control calls, LLM chat streams, ComfyUI HTTP traffic, and ComfyUI
WebSockets use separate client sessions and connection pools. A full or stale
WebSocket pool therefore cannot prevent `/free`, model lifecycle verification,
or ordinary ComfyUI HTTP calls. Connection acquisition has an explicit timeout,
streaming responses have a read-idle timeout, and WebSockets use bidirectional
heartbeats with deterministic relay-task cleanup.

Workflow request bodies are read with a dedicated byte limit. The FIFO also
limits both queued workflow count and total queued workflow bytes. If the
downstream client disconnects before submission, the queued item and its body
are removed. Once submission has started, the coordinator continues monitoring
the prompt so GPU ownership never becomes ambiguous, but the no-longer-needed
request body is released immediately after the upstream response.

## Workflow lease sequence

1. A workflow submission joins the same FIFO as chat work.
2. The coordinator stops granting new chat leases and drains active responses,
   including disconnected streams still running upstream.
3. The selected LLM adapter releases its GPU resources and verifies its own
   release postcondition.
4. The coordinator forwards the workflow and records the returned `prompt_id`.
5. ComfyUI history is polled until that exact prompt succeeds, fails, is
   cancelled, times out, or exceeds the consecutive transport-failure limit.
   The browser WebSocket is only a presentation path; it is not the
   authoritative completion signal.
6. Successful completion enters `comfy_ready`, the warm idle state. Consecutive
   workflows reuse the same ownership period.
7. Only an active LLM request at the FIFO head starts the reverse handoff.
   Active ComfyUI control requests drain first, `/free` must succeed, the LLM
   restore point is acquired, and readiness is verified before chat starts.

## Failure boundary

### Native KoboldCpp routing

With `--kobold-router-mode`, the FIFO serializes model-dependent requests through
their complete upstream responses. The broker first confirms ComfyUI cleanup,
then enters `llm_reserved` and forwards the request to KoboldCpp's native router.
The reservation is GPU permission, independent of model readiness. The native
router selects and loads the requested profile; no broker-selected restore
target or intermediate `initial_model` load is used. Before granting ComfyUI,
the broker always confirms an explicit unload, even after an uncertain router
response. A failed unload cannot be treated as available GPU memory.

Abort bypasses the model-dependent FIFO while a request is active, but holds a
short control reference that must drain before a handoff. Passive model discovery
returns a sanitized cached catalog during ComfyUI work or LLM requests. Generic
read-only metadata does not acquire GPU ownership. Router reservations exclude
new metadata operations during transitions, and the next generation waits for
existing metadata operations to finish. Startup still performs no broker-driven
backend loading; the stack's explicit readiness check seeds the cache.

The router accepts length-delimited request bodies, so the broker buffers only
the admitted request with a separate byte limit, supplies `Content-Length`, and
preserves the existing streaming response relay. Waiting disconnected clients
are removed; already-started upstream responses continue to drain. Direct
backend access is still outside the enforceable lease boundary.

### Ownership failures

Ownership is `unknown` during transitions where the current owner is no longer
confirmed. The coordinator never grants
the destination backend before the source release succeeds. In particular, a
failed or timed-out ComfyUI cleanup no longer attempts to reload the LLM: the
broker enters `error` and reports the sanitized cause through `/broker/status`.
It performs no background retry. A later active LLM request can make one new
cleanup-and-restore attempt; no idle, workflow, lifecycle, or shutdown event
may load KoboldCpp.

Status includes coordinator health, dispatcher liveness, state age, stall
classification, active transport counts, and queued workflow bytes. The local
stack supervisor treats the status endpoint as liveness and `healthy` as
readiness: degraded status is logged but never converted into an implicit
stack shutdown.

This is logical VRAM ownership through application lifecycle APIs. It cannot
revoke CUDA device access from a misbehaving process. Hard device exclusion
requires an external process/container supervisor or an operating-system GPU
device policy in addition to this broker.

### TabbyAPI lifecycle adapter

TabbyAPI uses the existing explicit lifecycle contract and request-driven startup,
without KoboldCpp's native-router reservation branch. Its adapter consumes load
SSE through EOF and verifies the resulting model card, and awaits unload before
confirming absence of the container. An interrupted load may continue upstream;
an unconfirmed mutation is retained as an adapter error and cannot be cleared by
a passive 503 response. No new authentication, routing, or cancellation policy
is introduced. See the [setup limitations](USER_GUIDE.md#tabbyapi--exllamav3) for
native defaults needed to retain offload settings across API reloads.

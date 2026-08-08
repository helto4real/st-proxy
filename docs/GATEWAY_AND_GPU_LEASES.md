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
| Control | `/interrupt`, `/queue`, job-cancel routes | Allowed only while ComfyUI owns the GPU; holds a short control lease |
| Lifecycle | `/free` | Broker-internal; external calls get HTTP 403 |
| Known UI mutation | uploads, settings, userdata, history, reviewed Helto privacy/keystore operations | Transparent passthrough |
| Unknown mutation | custom extension route | HTTP 403 in strict mode |

`--allow-unknown-comfy-routes` changes only the final row to transparent
passthrough. It is a compatibility escape hatch, not an ownership guarantee.
Review custom routes before enabling it because an extension may allocate GPU
memory without submitting a normal ComfyUI prompt.

The reviewed `helto-privacy` keystore operations, Helto Director timeline
encryption/decryption routes, and authenticated H3 preview decryption route are
allowlisted individually. Their namespaces are not allowlisted as prefixes, so
a new mutating route still fails closed until it has been reviewed.

For compatibility with ComfyUI backends that expose only the legacy route
names, the gateway translates the frontend aliases `/api/prompt`,
`/api/settings`, `/api/userdata`, and `/api/users` to their unprefixed upstream
equivalents. Other `/api/...` routes are preserved because endpoints such as
`/api/jobs` and `/api/assets` are genuine API routes.

The transport preserves the browser's raw percent-encoded path. This is
required for nested userdata names: ComfyUI sends a path such as
`workflows/example.json` as the single route segment
`workflows%2Fexample.json`.

## Workflow lease sequence

1. A workflow submission joins the same FIFO as chat work.
2. The coordinator stops granting new chat leases and drains active responses,
   including disconnected streams still running upstream.
3. The selected LLM adapter releases its GPU resources and verifies its own
   release postcondition.
4. The coordinator forwards the workflow and records the returned `prompt_id`.
5. ComfyUI history is polled until that exact prompt succeeds, fails, is
   cancelled, or times out. The browser WebSocket is only a presentation path;
   it is not the authoritative completion signal.
6. Successful completion enters `comfy_ready`, the warm idle state. Consecutive
   workflows reuse the same ownership period.
7. A chat at the FIFO head or the idle deadline starts the reverse handoff.
   Active ComfyUI control requests drain first, `/free` must succeed, the LLM
   restore point is acquired, and readiness is verified before chat starts.

## Failure boundary

Ownership is `unknown` during every transition. The coordinator never grants
the destination backend before the source release succeeds. In particular, a
failed or timed-out ComfyUI cleanup no longer attempts to reload the LLM: the
broker enters `error`, rejects queued GPU work, and reports the sanitized cause
through `/broker/status`.

This is logical VRAM ownership through application lifecycle APIs. It cannot
revoke CUDA device access from a misbehaving process. Hard device exclusion
requires an external process/container supervisor or an operating-system GPU
device policy in addition to this broker.

# Central model profiles

`config/model-profiles.json` supplies missing sampling parameters for
`POST /v1/chat/completions`. The priority is:

1. The caller's explicitly supplied value.
2. The matching model profile's defaults.
3. The backend's own defaults.

This applies to any harness using that endpoint, including Pi, OpenCode,
Open Grok and Dreamchat. A harness that sends its own defaults wins too;
omit a parameter in the harness to use the central default. Explicit zero,
false, null and invalid values are preserved for the backend to interpret
or reject. Unknown request fields, tools, messages and output limits survive.

## Activation

`st-stack.zsh` supplies the repository's `config/model-profiles.json` by default,
independent of the working directory. Override it with
`ST_PROXY_MODEL_PROFILES=/absolute/path/profiles.json`. An explicitly empty
`ST_PROXY_MODEL_PROFILES` disables profiles in the stack.

Direct CLI startup is opt-in: use `--model-profiles /absolute/path/profiles.json`
or the environment variable. `--model-profiles ''` overrides and disables an
environment setting. Files are read and validated once at startup, before
listeners or backend sessions open. A bad file prevents startup. Changes take
effect at the next proxy start; there is no hot reload or online lookup.

## Format

```json
{
  "version": 1,
  "profiles": [
    {
      "id": "my-model",
      "models": ["prism/my-native-model-id"],
      "defaults": {"top_k": 20, "repetition_penalty": 1.0},
      "thinking": {"temperature": 1.0, "top_p": 0.95},
      "non_thinking": {"temperature": 0.7, "top_p": 0.8}
    }
  ]
}
```

Matches use exact `backend/native-model-id` keys, including in single-backend
mode where the caller sends only the native ID. No wildcards or model-name
guessing are used. Managed Prism/Strata requests with an omitted model use
their resolved target. Single-backend Tabby requests need an explicit model
to match. Unknown models and unsupported routes retain their existing behavior.

Supported backends are `prism`, `tabbyapi`, `sc117`, `orca`, `sc117_128k` and
`orca_128k`. KoboldCpp and Ollama are outside this first profile version.
Model discovery stays dynamic: profiles neither add models to `/v1/models`
nor load them. Other endpoints, including `/v1/messages`, are not modified.

Supported profile parameters:

| Parameter | Profile validation |
| --- | --- |
| `temperature` | Finite number, 0–2 |
| `top_p` | Greater than 0, at most 1 |
| `top_k` | Nonnegative integer; Strata requires 1–64 |
| `min_p` | 0–1 |
| `repetition_penalty` | Greater than 0 |
| `presence_penalty`, `frequency_penalty` | −2–2; Strata requires 0–2 |
| `reasoning_budget_tokens` | Optional nonnegative integer; omitted in all shipped profiles |

The proxy translates `repetition_penalty` to Prism's `repeat_penalty`;
Strata and Tabby use `repetition_penalty`. Caller aliases `repeat_penalty`,
`repetition_penalty` and `rep_pen` suppress the profile default and supply
the native field if it is absent. An explicit native field wins even when
null; Tabby's native `rep_pen` alias takes priority over `repeat_penalty`.

Duplicate model matches, duplicate JSON keys, unsupported fields and invalid
profile values fail startup with a configuration error. Optional metadata
fields are `sources` (URL list), `checked_at`, `installed_revision` and `notes`
(strings). Metadata documents provenance and does not change inference.

## Thinking and budgets

Profiles never set thinking on/off, reasoning effort or an output-token limit.
Mode-specific sampling is used only with explicit, recognized and unambiguous
caller controls. Absent, null, unrecognized or conflicting controls select
only `defaults`; backend thinking settings are not guessed.

Recognition follows the supported backend controls: `reasoning_effort`,
`chat_template_kwargs.enable_thinking` / `reasoning_effort`, and, where supported,
`reasoning.effort`, `reasoning.enabled`, `enable_thinking` and `template_vars`.
For example Strata ignores a true `chat_template_kwargs.enable_thinking`, so
that alone cannot select thinking defaults. Prism recognizes top-level `none`
as off, not `off`, and its Bonsai templates do not support `high`.
Thinking controls themselves are never rewritten by profiles.

**There is no default thinking budget.** To opt into a central fallback later,
add `reasoning_budget_tokens` to a profile's `defaults`. Any caller thinking
control or budget alias suppresses that fallback, including null, zero,
`reasoning.max_tokens` and template controls. A budget alone does not choose
the thinking/non-thinking sampling block. Zero is passed through; its backend
meaning is not converted to thinking off.

Existing Tabby request adaptation remains in place after profile merging:
an explicit effort and output limit can already derive a Tabby budget.
That is caller-driven existing behavior, not a budget from the shipped profiles.
Budget aliases retain the existing adapter/backend interpretation; the profile
layer does not introduce a new cross-backend reasoning protocol.

## Shipped defaults and sources

Checked online on 2026-10-10. These are manufacturer-based starting points for
coding and tool use, not a locally measured optimum or a latency guarantee.
The JSON records exact IDs, source links and installed Bonsai weight revisions.

| Models | Explicit thinking | Explicit non-thinking | Common defaults |
| --- | --- | --- | --- |
| SC117 and Orca Flash Next, both 32k and 128k backends | temperature 1, top_p 0.95, min_p 0, presence 0 | temperature 0.7, top_p 0.8, min_p 0, presence 1.5 | top_k 20, min_p 0, repetition 1, frequency 0 |
| Bonsai 2 original, Abliterated, Abliterated v2 MTP, TwinTurbo Residual | temperature 1, top_p 0.95, min_p 0.05, presence 0 | temperature 0.7, top_p 0.8, min_p 0, presence 1.5 | top_k 20, repetition 1, frequency 0 |

- [Qwen Flash Next recommendations](https://huggingface.co/Qwen/Qwen3.8-Flash-Next#best-practices)
  supply the SC117/Orca defaults. No separate sampling recommendation was found
  for those derivatives; inheritance is a project choice.
- [PrismML Bonsai 2 recommendations](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf#best-practices)
  supply original Bonsai's mode-specific values, including thinking min_p 0.05.
- [Older Abliterated](https://huggingface.co/BoldingBuilds/Ternary-Bonsai-2-27B-Abliterated-PTQ1_0-GGUF)
  inherits Bonsai sampling. Its publisher recommends disabling thinking because
  of runaway reasoning; the profile leaves that decision to the caller.
- [Abliterated v2](https://huggingface.co/BoldingBuilds/Ternary-Bonsai-2-27B-Abliterated-v2-PQ2_0-MTP-GGUF)
  and [TwinTurbo](https://huggingface.co/shunmoridev/Qwen3.8-27B-TwinTurbo-Residual-Bonsai-2-PQ2_0-GGUF)
  specify temperature 1, top_k 20 and top_p 0.95. Remaining values inherit
  Bonsai's recommendations as a project choice. No recommended effort is forced.

Frequency penalty 0 is a neutral project default. Backend transport is checked
against local Strata/Tabby source and the installed
[Prism runtime revision](https://github.com/PrismML-Eng/llama.cpp/blob/adfffbe/tools/server/server-common.cpp).
Debug logging records profile ID, selected mode and names of inserted fields;
it does not log prompts or message content.

## Verification boundary

Synthetic tests cover precedence, aliases, validation, per-mode defaults,
single/all-backend routing, streaming and the existing GPU queue ordering,
plus CLI and fake-stack configuration. They do not establish model quality,
actual runtime sampling results, speed or VRAM use. GPU coordination and MCP
generation keep their existing implementation.

# Local image generation over MCP

The optional MCP listener exposes approved image workflows, with optional references. It uses the
running broker's existing FIFO and GPU coordinator, without starting another broker.

## Enable

Install `python -m pip install -e '.[mcp]'`, then configure these environment
variables before starting your normal broker or `st-stack.zsh`:

`st-stack.zsh` enables MCP on port `8190` by default, reads workflows from the
project's `workflows/`, and stores image copies in
`/run/media/veracrypt1/mcp-images`. The environment variables below override these
defaults. Direct broker startup still requires an explicit MCP port and output
directory.

| Setting | Purpose |
| --- | --- |
| `ST_PROXY_MCP_PORT` | Enables `http://127.0.0.1:<port>/mcp`; choose an unused port, e.g. `8190` |
| `ST_PROXY_MCP_WORKFLOWS_DIR` | Optional override for the directory containing `*.manifest.json` and their API exports; defaults to the project's `workflows/` |
| `ST_PROXY_MCP_OUTPUT_DIR` | Directory for copies of generated images |
| `ST_PROXY_MCP_INPUT_DIR` | Allowed incoming reference directory; defaults to `/run/media/veracrypt1/mcp-inputs` |
| `ST_PROXY_MCP_TOKEN` | Optional: require this Bearer token from MCP clients when set |

The port and directories also have `--mcp-port`, `--mcp-workflows-dir`, and
`--mcp-output-dir` / `--mcp-input-dir` flags. The MCP token is environment-only and excluded from config
representations. MCP is disabled without a port. Its bind address is always
IPv4 loopback, independently of the other listeners.

Configure your client's Streamable HTTP connection with the MCP URL. No token
is required by default; when `ST_PROXY_MCP_TOKEN` is set, also send
`Authorization: Bearer <ST_PROXY_MCP_TOKEN>`. V1 has no stdio bridge. Host and
Origin checks remain enabled in both modes. Other local programs can access
the MCP listener without credentials when no token is configured.
This interface does not provide an OS sandbox that
prevents local programs from accessing other ports or files.

## Workflow manifests

The supplied `workflows/image_generation.manifest.json` references
`image_generation.json`. It exposes `prompt` (mapped to `positive_prompt`),
`aspect_ratio` (default `16:9`) and `max_side` (default `1600`) on node `1`, and
uses node `3` for Helto output. Max side accepts 256–3200 pixels in steps of 32,
matching the export's sizing controls. The virtual `mode` parameter defaults to
`standard`; its operator-owned YAML controls the model, sampling and LoRAs described
below. Remaining settings, including seed, come from the unchanged API export.

For text-to-image, `describe_workflow("image_generation")` returns
`prompts/prompt-enhance-scene.md` verbatim as `prompt_guidance`. The agent reads it
before composing the final `prompt` (up to 12,000 characters), then calls
`generate` or `generate_batch`. Guidance about returning only the rewritten prompt
applies to that parameter, not to the agent's entire turn.

With the editable installation above, the default directory is `workflows/`
at the repository root, regardless of the shell's working directory. Store each
API export and its `*.manifest.json` there, or select another directory with
`ST_PROXY_MCP_WORKFLOWS_DIR` / `--mcp-workflows-dir`.

This example uses ordinary ComfyUI image outputs:

```json
{
  "id": "portrait",
  "name": "Portrait",
  "description": "Create a portrait from a text description.",
  "workflow": "portrait.json",
  "parameters": {
    "prompt": {
      "type": "string",
      "required": true,
      "private": true,
      "minLength": 1,
      "maxLength": 4000,
      "target": {"node": "12", "input": "text"}
    },
    "seed": {
      "type": "integer",
      "default": 42,
      "minimum": 0,
      "maximum": 9007199254740991,
      "target": {"node": "27", "input": "seed"}
    }
  },
  "outputs": [{"node": "30", "adapter": "comfy_images"}]
}
```

Use your export's actual node IDs. Workflow files must resolve inside the catalog
directory. Restart after changes. Invalid definitions are logged and unavailable;
other workflows remain usable. Duplicate IDs disable all definitions with that ID.

Only mapped inputs, explicit mode targets and selected reference/output adapters' contracts
are checked.
Other nodes, classes, inputs, links and metadata stay opaque and are forwarded
unchanged. Adding nodes does not require a manifest update. ComfyUI still
performs its own runtime validation.

Parameter types: `string`, `integer`, `number`, `boolean`. Constraints: `enum`,
`minimum`, `maximum`, `minLength`, `maxLength`. Optional parameters need a `default`.
Integer parameters can also specify a positive integer `multipleOf` step.
Private parameters are required and cannot have defaults. Unknown agent
parameters are rejected. Unspecified string length is limited to 65,536 characters.

`private: true` omits a parameter from result metadata; it does **not** encrypt
the value sent to ComfyUI. Like Dreamchat, st-proxy writes prompt text directly
into the mapped input and submits the workflow to `/prompt`. No separate prompt
encryption request or Helto privacy token is needed. Unmapped values, including
encrypted negative prompts and privacy settings, remain unchanged in the export.

## YAML generation modes

Both supplied manifests reference `"modes": "modes/qwen-image.yaml"`. Edit
`workflows/modes/qwen-image.yaml` to change `quick draft` and `standard`. It is read once
per catalog at proxy startup, shared by both workflows; edits require a restart
to take effect. There is no per-request file reading or hot reload.

Quick draft keeps the API export's values: 8 steps, cfg 1, `res_multistep`,
`simple`, and `qwen_image_2.1_turbo_bf16.safetensors`. Standard uses 40 steps,
cfg 2.5, `er_sde`, `beta`, and `qwen_image_2.1_int8_convrot.safetensors`.
Both profiles retain the **disabled** LoRA at strength 1. Quick draft keeps
`sigmas: ["13", 0]` and exposes node 13's sigma list in YAML. Standard removes
node 1's `sigmas` input. The disconnected ManualSigmas node remains in the graph.

Both profiles expose `use_zero_negative_conditioning` under node `"1"` in YAML.
Standard sets it to `false`, allowing the workflow's negative prompt to be used
with CFG above 1. Quick draft retains `true`, which uses zeroed negative
conditioning. Change the boolean per mode and restart to apply it.

The mode descriptions tell the agent to use standard for normal generation and
finished images, and quick draft for previews, composition tests or requests
that prioritize speed.

With manual sigmas connected, the list determines the actual step count and
sampling schedule; editing `steps` or `scheduler` alone does not change those.
Without that connection, the current workflow's `sampling_schedule: comfy`
uses the configured steps and scheduler.

The YAML structure is `version: 1`, a `default` mode name, and a `modes` mapping.
Each mode has a `description` and optional `set` / `remove` mappings. For example:

```yaml
version: 1
default: standard
modes:
  "quick draft":
    description: Use for quick previews, composition tests and exploring ideas.
    set:
      "1":
        steps: 8
        cfg: 1
        sampler: res_multistep
        scheduler: simple
        sigmas: ["13", 0]
  standard:
    description: Default for normal image generation and finished images.
    set:
      "1":
        steps: 40
        cfg: 2.5
        sampler: er_sde
        scheduler: beta
    remove:
      "1": [sigmas]
```

Use quoted node IDs and the API export's exact input names (including
`diffusion_model`). `set` replaces entire input values, including LoRA objects;
it does not merge their fields. To add a LoRA, explicitly add `lora_2` under
node `"11"`, with its complete `on`, `lora`, and `strength` values. Unlisted inputs
retain their export values. `remove` removes only the named inputs from the job
copy; removing an already absent input is harmless. Each mode starts from a
fresh export copy, never from the preceding job or another mode.

YAML uses safe loading, unique string keys and finite JSON-compatible values.
Use `true` / `false` for booleans; `on` remains a string key. Files must stay
within the workflow directory and fit the workflow byte limit, including expanded
content. Duplicate keys, conflicting set/remove patches, missing target nodes,
and overlaps with agent parameter mappings or adapter-owned reference/output
controls make the affected workflows unavailable. New inputs on existing nodes
are allowed. The proxy does not check model/LoRA inventories or validate unrelated
nodes and connections; ComfyUI performs its normal runtime validation.

`describe_workflow` exposes available names, descriptions and default through
`parameters.properties.mode`. The agent chooses a mode, not arbitrary graph
patches. To add `high quality` later, add a `"high quality"` entry under `modes`
with its description and set/remove patches, then restart the proxy. No Python
change is required. Each mode starts from the export, so copy any patches you
want to share with standard. Manifests without a modes file retain their existing behavior.

For `generate`, pass `"parameters": {"prompt": "...", "mode": "quick draft"}`.
Omitting `mode` selects `standard`. Mode names include spaces exactly as written
in YAML; the old `turbo` and `quality` names are no longer configured.
Each batch item can select its own mode, either as a flat parameter object or
inside `parameters` when using references. Modes are applied before ordinary
parameters and reference/output adapters. All items are prepared before queueing;
mixed modes retain the original batch order and GPU reservation, without sorting
by model. ComfyUI may need to load a different image model between items.

The resolved mode appears in public result parameters and job identity. Omitted
mode and explicit default are equivalent for retries; changing a mode under an
existing job ID returns a conflict. Restarting clears the existing in-memory job
registry as before.

## Reference scenes and prompt guidance

`qwen_scene` is separate from the original text-to-image workflow. It allows zero
to ten ordered reference images and a final prompt of up to 12,000 characters.
It retains the existing aspect ratios, default max side 1600 and maximum 3200.
Qwen uses `main controls` for the new scene's canvas.

Its manifest adds:

```json
{
  "prompt_guidance": "prompts/reference-aware-scene.md",
  "references": {
    "adapter": "helto_root_images",
    "generator": "1",
    "loader": "9",
    "inputs": ["image 1", "image 2", "image 3", "image 4", "image 5",
               "image 6", "image 7", "image 8", "image 9", "image 10"]
  }
}
```

`describe_workflow` returns the UTF-8 guidance file verbatim as `prompt_guidance`;
listing tools/workflows does not embed it. The file must stay within the workflow
catalog and fit within 64 KiB. The agent applies it to `parameters.prompt` and then
calls the generation tool. st-proxy does not make an extra LLM rewriting request.

The reference adapter validates only the selected generator's inputs object and
loader template contract. Optional reference sockets may be absent in the export.
For each job it clears only the configured reference slots, clones the selected
`HeltoLoadImageFromRoot` with collision-free node IDs, and connects the ordered
images. Unmapped nodes and loader options remain opaque.

Create/mount the input directory before use. For a selected workspace image,
copy it to a unique filename there before calling `register_image`. Absolute paths
must resolve within the configured input or output directory. PNG, JPEG and WebP
are detected from contents, limited to 10 MiB, EXIF-oriented, and converted to PNG
using the first frame. The normalized PNG must also fit within 10 MiB.

Registration uses the configured ComfyUI URL's
`/helto_load_image_from_root/store` endpoint with `root` set to the MCP output
directory and `subfolder` set to `references`. It uses CPU/storage without acquiring
the GPU. ComfyUI and st-proxy must share this local filesystem. Missing/unreadable
files, rejected uploads and invalid responses fail registration without queueing
any generation. Source files are not changed.

The result includes an opaque `image_id`, `width`, `height`, `mime_type` and
`preview_path`. IDs belong to one workflow and expire on proxy restart. Stored
files persist; re-register them after restart. No database or automatic cleanup
is introduced. Modified or missing registered files are rejected before queueing.

Pass IDs in exactly the order used by the prompt's `<image1>`, `<image2>` references:

```json
{
  "job_id": "scenes-001",
  "workflow_id": "qwen_scene",
  "items": [
    {"parameters": {"prompt": "Place the character from <image1> beside a lake."},
     "references": ["REGISTERED_IMAGE_ID"]},
    {"parameters": {"prompt": "Place the character from <image1> in a snowy forest."},
     "references": ["REGISTERED_IMAGE_ID"]}
  ]
}
```

Single `generate` calls accept the same optional `references` list alongside
`parameters`. Batch items can use either the new envelope shown above or the
original flat parameter objects. Each item has its own reference order; all
parameters and references are checked before admission. Content hashes and order
are part of job identity, so registering the same image again does not force a new
job. Reordering or changing references with an existing job ID returns a conflict.

The Pi sandbox's `/skill:qwen-scene` loads the scene skill on demand. It reads the
workflow guidance, establishes reference roles, registers images and prepares all
prompts before one batch. The launcher mounts the input directory read/write,
results read-only, and only this skill directory read-only. Set input/output
environment overrides consistently for both st-proxy and Pi. Launch a new Pi
sandbox session to pick up mounts; a broker restart loads new workflows and tools.
This implementation does not restart either service automatically.

## Outputs

`comfy_images` reads standard `images` records from the selected node's history
and downloads via `/view`. It supports PNG, JPEG and WebP without requiring a
particular output-node class.

`helto_saved_images` requires `HeltoSaveImageAdvanced`, `save_image=true`,
`pause_mode=false`, and an absolute destination folder. It sets only
`reference_only=true` in the job's graph copy, preserving privacy and folder
settings. The broker resolves the returned single-use references immediately and
copies those PNGs into its output directory. Sources must remain inside the
save node's selected destination root. The agent cannot change save-node controls.

Helto references expire after 60 seconds. Resolution runs before file transfers,
even if the caller disconnected. Expired or consumed tokens produce a result
failure, never another generation. This adapter requires ComfyUI on the same
machine and source paths readable by the broker. Missing volumes and unreadable
files produce job failures.

Up to 64 images of at most 64 MiB each are copied per workflow execution (per
item for a batch). Files are staged in a
private temporary directory and published together after every transfer succeeds.
Source files are untouched. Results contain absolute paths and MIME types,
without private prompt text, envelopes, backend URLs or reference tokens.
Completed files persist until you remove them; choose the result directory
according to your storage/privacy needs.

## Tools and recovery

| Tool | Arguments and behavior |
| --- | --- |
| `list_workflows` | Lists approved IDs and descriptions |
| `describe_workflow` | `workflow_id`; returns parameter schema, defaults, reference limits and optional prompt guidance |
| `register_image` | `workflow_id`, `path`; stores a reference without GPU acquisition, returns an image ID and preview path |
| `generate` | `job_id`, `workflow_id`, `parameters`, optional ordered `references`; waits until images are saved |
| `generate_batch` | `job_id`, `workflow_id`, `items`; each item is a parameter object or `{parameters, references}` for the same workflow; waits for all results |
| `get_job` | `job_id`, `wait` (default `true`); waits or returns current status |

**Prefer `generate_batch` when multiple images are needed.** The agent prepares
all prompts in one LLM turn and waits for the entire batch. Consecutive FIFO
positions keep ComfyUI on the GPU between images, avoiding repeated LLM reloads.
Existing requests ahead of the batch keep their position; later LLM requests
wait until all its workflow executions finish. GPU handoff uses the unchanged
dispatcher and lifecycle policy.

Example `generate_batch` arguments:

```json
{
  "job_id": "landscapes-001",
  "workflow_id": "image_generation",
  "items": [
    {"prompt": "A red cottage beside a lake at sunset", "aspect_ratio": "16:9", "max_side": 1024},
    {"prompt": "A forest path in morning mist", "aspect_ratio": "3:4", "max_side": 1024}
  ]
}
```

Each item uses the workflow's normal mappings and defaults. No graph validation
is added for unmapped nodes. All parameters and queue capacity are checked before
any item is enqueued; admission is all or none. The batch must be non-empty and
fit the existing queue count and byte limits (32 workflow executions by default).
Seed is not changed implicitly: repeating identical parameters can produce the
same image. Different seeds require a configured seed mapping.

Batch results contain ordered `items` with zero-based `index`, public parameters,
status, images and any error. Each result is saved as soon as its generation
finishes, so Helto references do not wait for the rest of the batch. Runtime
failures are reported per item; other items continue and successful files remain
available. Batch status is `succeeded`, `partial` or `failed`; partial success also
sets the MCP error flag. Inspect the item results before deciding what to retry.

Choose `job_id` before calling `generate` or `generate_batch`: 1–128 letters,
digits, underscores or hyphens. A UUID is suitable. The same ID and normalized parameters attach to the
existing job. For batches the full ordered item list is part of that identity.
Different parameters or switching between single and batch with that ID return
a conflict. Completed failures are retained too; retries never silently regenerate
an image.

After timeout or disconnect, call `get_job` with the original ID. Cancelling an
MCP request cancels the wait, not the job. V1 has no job cancellation tool.
The in-memory registry lasts until broker restart; unknown IDs return an error.
Saved image files survive restart.

States: `preparing`, `queued`, `generating`, `saving`, `succeeded`, `failed`.
Batch results additionally support `partial` when only some items succeed.
Errors include a category, sanitized message and job ID when available. Existing
coordinator queue count/byte limits apply; pre-enqueue MCP work is also bounded
by the workflow count limit.

With a local LLM, the agent waits for `generate` or `generate_batch` without
running the model.
The coordinator drains active LLM responses, unloads the model and runs the image
through the shared FIFO. MCP returns the saved result while ComfyUI may still own
the GPU. Only the agent's **next active LLM request** triggers cleanup and model
restoration. Metadata and MCP status calls never restore the LLM. Waiting for
`llm_ready` inside the tool would deadlock request-driven restoration.

Allow enough client tool timeout for queueing plus all batch generations, and
enough LLM request timeout for model restoration. The broker does not reconfigure clients.

## Verification

Install `python -m pip install -e '.[dev]'`, then run `python -B -m pytest` and
`ruff check .`. Tests use a sanitized copy of the supplied graph, temporary files,
and registered servers on dynamic loopback ports. They require
no real keystore, model, mounted volume, ComfyUI or LLM service.

Real agent/model/workflow acceptance is separate and must be explicitly authorized.
SDK tests are not proof of a particular agent's configuration or live GPU behavior.

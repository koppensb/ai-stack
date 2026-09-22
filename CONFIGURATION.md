# Code and configuration guide

This guide explains the files that implement the stack. Inline comments describe
local decisions; README.md covers host preparation and operations, and INSTALL.md
covers the installer. Values below describe the checked-in configuration, not a
running deployment's saved settings.

## Where to change a setting

| File or location | Responsibility | When changes take effect |
| --- | --- | --- |
| `.env.example` / deployed `.env` | Deployment paths, credentials, image references, resource limits and service options | Recreate affected containers; rebuild for build arguments |
| `docker-compose.yml` | Networks, mounts, service arguments, environment mappings and health checks | Recreate affected containers |
| `llama_cpp_rocm.dockerfile` | ROCm SDK and compiled llama.cpp server/downloader | Rebuild image and recreate both llama.cpp services |
| `comfyui_rocm.dockerfile` | Latest stable ComfyUI, GGUF nodes and completion-time VRAM cleanup patch | Rebuild image and recreate ComfyUI |
| `config/llama-cpp/models.ini` | Main router model IDs, downloads and per-model loading options | Prefetch new assets, then restart/recreate the router |
| `config/llama-cpp-image/models.ini` | Dedicated image-prompt router preset | Prefetch new assets, then restart/recreate that router |
| `config/openwebui/apply_model_parameters.py` | Four workspace presets, English system prompts, sampling defaults and public read grants | Initial signup; explicit rerun for later prompt/default changes |
| Open WebUI database | Saved admin settings, workspace models, users, access grants and initialization markers | Apply the relevant helper or edit through the UI; restart to clear cached settings |
| `config/nginx/nginx.conf` | TLS listeners and proxy routes | Validate with `nginx -t`, then reload/restart NGINX |
| `config/searxng/settings.yml` | Search engine allowlist, result formats and upstream timeouts | Restart SearXNG |
| `config/prometheus/*.yml` | Scrapes, discovery mapping and alert rules | Validate with `promtool`, then reload/restart Prometheus |
| `config/grafana/provisioning/` | Internal datasource and file-managed dashboard | Dashboard files are scanned every 30 seconds; restart for datasource/provider changes |
| `config/llama-metrics-discovery/llama_metrics_discovery.py` | Main-router metrics discovery and VRAM coordination | Restart the bind-mounted Python process |
| `ai-stack.service` | Start/stop prebuilt containers at host boot/shutdown | Reinstall changed unit and run `systemctl daemon-reload` |

Ordinary Compose commands allow exported shell variables to override `.env`.
The installer clears the inherited environment for Compose so saved deployment
values win. `.env` is parsed by Compose, never sourced as shell code. Required
variables use `${NAME:?...}` so missing values fail validation. Only explicitly
mapped variables reach each service.

Open WebUI can retain admin settings in its database even after environment
changes. The startup wrapper supplies defaults; setup helpers perform explicit
writes to persistent configuration. Editing a system-prompt constant does not
reset presets after their initialization marker has been saved.

## Install, update, and download helpers

- `install_ai_stack.sh` checks the host, serializes installations with a lock,
  prepares packages/runtime, copies managed files, maintains credentials, builds
  images, downloads weights, and optionally starts/verifies services. Updates
  are backed up but are not rolled back automatically on failure.
- `scripts/deployment_files.txt` is a machine-read copy allowlist: one relative
  file path per line. Do not add comments or blank lines; the installer treats
  every line as a filename. Source-only setup helpers and this guide need not
  be deployed. `apply_model_parameters.py` is included because startup calls it.
- `scripts/download_models.py` reads both router INI files, handles optional MTP
  or draft assets, and runs the built `llama download` command without starting
  inference. Its `IMAGE_ASSETS` manifest supplies ComfyUI weights. Completed
  nonempty image files are reused without checksum verification; `.part` files
  resume and are renamed only after successful download.
- `config/openwebui/start.py` loads graph/mapping JSON into environment defaults,
  serializes the terminal connection, launches the preset helper, and replaces
  itself with the upstream startup process so shutdown signals reach it.
- `config/openwebui/apply_qwen_images.py` writes the managed image workflows and
  external task model into the database. API connection URLs and keys are
  parallel arrays; their indices must stay aligned. Existing unrelated endpoints
  are retained.
- `config/openwebui/apply_open_terminal.py` merges by connection ID or URL. It
  updates credentials while retaining existing grants and an intentional disable.
- `config/openwebui/verify_qwen_workflows.py` compares API graph inputs and links
  with ComfyUI's `/object_info`. It checks model availability but does not run
  generation or prove that weights fit into GPU memory.
- `scripts/tests/test_model_preset_access.py` uses in-memory store doubles to
  check sharing migration, retries, and preservation of edits. Run it with
  `python3 -B -m unittest discover -s scripts/tests -v` from the source root.

## Model ownership and access

The first administrator owns newly created workspace presets. Their stable IDs
are `ai-stack-coding`, `ai-stack-allround`, `ai-stack-creativ`, and
`ai-stack-image-generation`. The management marker prevents overwriting an
unrelated model with a colliding ID. Display names are not backend API IDs.

Initialization, sharing, and prompt caching use separate database markers:

- `ai_stack.model_presets.initialized`: managed presets have been initialized.
- `ai_stack.model_presets.public_read_v1`: read grants were verified for presets
  and their base models.
- `ai_stack.model_presets.prompt_cache_v1`: cache-only migration has been verified.

The second marker lets older installations receive sharing without resetting
edited prompts or sampling values. `user:*` read grants cover signed-in users;
they do not add anonymous access or new editing privileges. Existing grants are
retained. Account approval and feature-specific permissions still apply. Terminal
access is configured separately because its workspace is shared among its users.

Explicitly rerunning the preset script reapplies managed prompts, names, sampling
values and read grants. Normal restarts skip completed migrations. A successful
partial write is retained after failure, and the missing work is retried before
marking the migration complete.

## Workflow JSON: two distinct formats

JSON does not support comments. Keep executable graphs and mapping arrays valid
JSON; use this guide and the existing editor Note nodes for explanations.

### Open WebUI API graphs

`config/openwebui/qwen-image-2.1_image.json` and
`config/openwebui/qwen-image-2.1-edit_image.json` map node IDs to `class_type`
and named `inputs`. A link such as `["9", 0]` means output index 0 of node 9.
Node IDs are local to that graph; request mappings refer only to the API graphs.

| Nodes | Role |
| --- | --- |
| 1, 2, 3 | Load diffusion GGUF, Qwen text encoder, and VAE respectively |
| 6 | `TextEncodeQwenImage21`: positive/negative conditioning and reference-sized empty latent |
| 8 (generation only) | Allocate an empty output latent |
| 9 | Sample with 40 steps, CFG 1, Euler/simple, and denoise 1.0 |
| 10, 11 | Decode and save the result |
| 12 (editing only) | Load the uploaded reference |

The `*_nodes.json` mappings insert model filenames into node 1's `unet_name`,
prompts into node 6's `prompt`, and seeds into node 9's `seed`. Generation also
maps steps to node 9 and width, height and batch size to node 8. Editing maps the
uploaded image into node 12's `image`; its 40 steps remain fixed in the graph.

The edit encoder takes `images.image_1`, preserves the reference aspect ratio,
and resizes to roughly `resolution * resolution` pixels, rounded to multiples
of 32. `resolution=1024` yields about one megapixel. Its third output is the
empty sampling latent with matching dimensions. `IMAGE_EDIT_SIZE` is required
by Open WebUI but does not determine output dimensions. Extra uploads are not
automatically mapped. The verifier understands the encoder's dynamic image inputs.

Both paths use one diffusion GGUF, a Qwen3-VL BF16 encoder including vision, and
the Qwen Image 2.1 VAE. Model filenames must match the download manifest.

### Native ComfyUI editor workflows

`config/comfyui/workflows/unsloth_qwen_image_2_1.json` and
`unsloth_qwen_image_2_1_edit.json` contain editor nodes, links and widget values.
They are mounted read-only in the `ai-stack` workflow folder. Use Save As elsewhere
for personal edits. They do not replace Open WebUI's API graphs.

Generation starts at 1024x1024. Native editing requires two uploaded references;
both feed `TextEncodeQwenImage21`, while the first determines the output canvas.
The same 1024-pixel resolution budget and sampling settings apply as in the API
workflow. Seed widgets randomize by default; retain a seed for comparisons.

### Grafana dashboard JSON

`config/grafana/provisioning/dashboards/files/llama-cpp-inference-stack.json`
contains panel descriptions, PromQL queries and selectors. Panels reference the
stable datasource UID `prometheus`; keep it aligned with datasource provisioning.
Rates and increases use counters to tolerate process resets. Optional speculative
metrics may be absent. Empty model panels can be normal when every model is
unloaded; the router-health panel uses the always-scraped coordinator instead.
This dashboard covers the main router, not the image-prompt router.

## VRAM coordination boundaries

The coordinator samples memory in MiB despite historical `_MB` option names.
Pressure is triggered by either enabled threshold: insufficient free memory or
excessive used percentage. Recovery requires both enabled criteria to clear their
hysteresis margins. In automatic probe mode the last working source is tried
first; a specific source disables fallback. GPU indices can differ by API/device
visibility and must identify the intended physical GPU.

Busy image work can trigger unloading of one verified-idle main-router model per
iteration. Idle ComfyUI can receive cache-release or full-unload requests for
llama.cpp demand. Queue/API failures are treated as unknown, not idle. Cooldowns
limit repeated attempts, including failed requests. These checks are polling:
a new request can arrive between the idle check and unload operation.

ComfyUI's `/free` acknowledges a request before the worker performs cleanup. The
bundled Dockerfile additionally patches the worker to release memory before
publishing completion, preserving history and status. That patch is guarded and
fails the build if the upstream anchor changes. Neither mechanism guarantees that
simultaneous requests or oversized model contexts fit the available GPU memory.

The coordinator's `/health` measures loop freshness; `/metrics` separately exposes
router reachability. `/targets.json` lists only loaded models. Prometheus injects
`autoload=false` so metric collection cannot load weights on its own.

## Current wiring to keep in mind

These are observations of the current settings, not changes made by the comment
review:

- The image-prompt service uses `LLAMA_CPP_GPU_LAYERS`, just like the main router;
  it is not CPU-only and its preset loads at startup. The coordinator currently
  monitors only the main router, so it cannot unload this service's model.
- `LLAMA_CPP_IMAGE_THREADS` controls both generation and prompt-processing CPU
  threads. `--batch-size` uses the shared `LLAMA_CPP_BATCH_SIZE` independently.
- All chat workspace presets use `Qwen3.8-27B`. Existing managed chat presets
  migrate once at startup, tracked by `ai_stack.model_presets.qwen38_chat_v1`,
  preserving custom prompts and settings.
- The Image Generation workspace preset, dedicated router and external task
  setting all use `Qwen3.5-4B`.

## Repository housekeeping

`.dockerignore` permits only the two Dockerfiles into build contexts because
sources are fetched during builds. `.gitignore` excludes credentials, caches,
backups and persistent service data. Empty `data/**/.gitkeep` files preserve
folders; they are not service configuration (the `data/loki` placeholder does
not mean Loki is deployed). `config/nginx/certs/placecertshere` documents the
required certificate pair and is not read by NGINX. Generated credentials,
certificates and runtime databases should not receive source-code comments.

## Prompt cache configuration

Both llama.cpp services pass `--cache-prompt`. Each managed Open WebUI preset
stores `custom_params.cache_prompt=true` as a boolean. The cache migration merges
this key without replacing unrelated custom parameters or edited system prompts;
it removes any conflicting top-level copy. Persistence is verified before saving
the migration marker. The startup helper skips a completed migration, while an
explicit preset reapply restores caching along with the other managed defaults.

The cache belongs to a resident model process and is lost on unload or restart.
It does not make answers deterministic or replace source verification. Stable
system prompts improve prefix reuse; dynamically rewritten context may reduce
it. See README.md for deployment and live verification steps.

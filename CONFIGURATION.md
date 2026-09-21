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

`config/openwebui/qwen-image-2512_image.json` and
`config/openwebui/qwen-image-edit-2511_image.json` map node IDs to `class_type`
and named `inputs`. A link such as `["9", 0]` means output index 0 of node 9.
Node IDs are local to that graph, not the IDs in the native editor files.

| Nodes | Role |
| --- | --- |
| 1, 2, 3 | Load diffusion GGUF, Qwen text encoder, and VAE respectively |
| 4, 5 | Apply sampling shift (3.1) and CFG normalization (strength 1.0) |
| 6, 7 | Positive and negative conditioning; the negative text starts empty |
| 8 | Allocate empty latent for generation; encode the reference image for editing |
| 9 | Sample with 40 steps, CFG 4, Euler/simple, and denoise 1.0 |
| 10, 11 | Decode the latent and save the result under the configured filename prefix |
| 12, 13 (editing only) | Load the uploaded image, then scale without cropping to about 2 megapixels |

The corresponding `*_nodes.json` files tell Open WebUI where to insert request
values. `type` identifies a request field, `node_ids` identifies targets, and
`key` names the target input. Renumbering a graph requires updating these mappings.

| Request input | Generation node/input | Editing node/input |
| --- | --- | --- |
| Model filename | 1 / `unet_name` | 1 / `unet_name` |
| Prompt | 6 / `text` | 6 / `prompt` |
| Width and height | 8 / `width`, `height` | Not mapped; derived from the uploaded image |
| Seed | 9 / `seed` | 9 / `seed` |
| Steps | 9 / `steps` | Fixed in the graph |
| Image count | 8 / `batch_size` | One reference-image result |
| Uploaded reference | Not used | 12 / `image` |

`reference.png` is a placeholder replaced by an upload; it need not exist during
schema validation. Edit node 13 uses `ImageScaleToTotalPixels` with Lanczos,
`megapixels: 2.0`, and `resolution_steps: 16`. It retains the complete frame;
rounding dimensions to multiples of 16 can slightly change the aspect ratio.
Increase or decrease `megapixels` in the API JSON to control resolution and
memory usage. `IMAGE_EDIT_SIZE` remains a valid explicit size for Open WebUI
request compatibility but does not determine this graph's output size.
The edit graph supplies the reference to both conditioning
nodes and VAE encoding. Full denoising here does not remove the separate reference
conditioning. Additional image uploads are not automatically mapped.

Model filenames must match the download manifest and installed assets. In
particular, the vision projector uses the text encoder's filename prefix for
GGUF discovery. Changing one filename can require changes in several files.

### Native ComfyUI editor workflows

`config/comfyui/workflows/unsloth_qwen_image_2512.json` and
`unsloth_qwen_image_edit_2511.json` contain editor `nodes`, `links`, widget values,
layout, and notes. They are mounted read-only in the `ai-stack` workflow folder.
Use Save As elsewhere for personal edits. These files do not replace Open WebUI's
API graphs and are not consumed by its request-field mappings.

The generation template starts at 1024x1024. The native editing template requires
two uploaded images: the first is resized to 1024x1024 and encoded as the output
latent, while the second is resized to 768x768 for additional conditioning.
The native resize nodes disable crop but use fixed dimensions; they are
independent of the aspect-preserving Open WebUI edit graph described above.
Both references feed positive and negative conditioning. Seed widgets default to
randomization; retaining a seed helps compare changes with the same settings.

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
- The Image Generation workspace preset references
  `mradermacher/Qwen3.5-4B-Q4_K_M.gguf`, while its router section and external task
  setting use `Qwen3.5-4B`. Verify the live `/v1/models` output and align the IDs
  before assuming this workspace preset can resolve its base model. Public
  grants do not repair a backend model-ID mismatch.

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

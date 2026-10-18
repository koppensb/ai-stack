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
| `playwright.dockerfile`, `config/playwright/` | Remote Chromium server, launch policy, seccomp profile and sandbox health check | Derive build inputs, rebuild and recreate browser |
| `config/web-egress/proxy.py` | Public-only HTTP/HTTPS proxy for browser traffic | Recreate proxy |
| `config/llama-cpp/models.ini` | Main router model IDs, downloads and per-model loading options | Prefetch new assets, then restart/recreate the router |
| `config/llama-cpp-small/models.ini` | Small-model task router preset | Prefetch new assets, then restart/recreate that router |
| `config/openwebui/apply_openwebui_parameters.py` | Global settings migrations, workspace presets, system prompts, sampling defaults and public read grants | Pending migrations on startup; explicit rerun for preset defaults |
| Open WebUI database | Saved admin settings, workspace models, users, access grants and initialization markers | Apply the relevant helper or edit through the UI; restart to clear cached settings |
| `config/nginx/nginx.conf` | TLS listeners and proxy routes | Validate with `nginx -t`, then reload/restart NGINX |
| `config/searxng/settings.yml` | Search engine allowlist, result formats and upstream timeouts | Restart SearXNG |
| `config/search-gateway/settings.toml` | Shared provider spacing, cooldowns, cache and request budget | Restart search-gateway |
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
  be deployed. `apply_openwebui_parameters.py` is included because startup calls it.
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
  updates credentials and restricts the managed connection to the explicit terminal
  group, retaining unrelated connections, other options and an intentional disable.
- `config/openwebui/verify_qwen_workflows.py` compares API graph inputs and links
  with ComfyUI's `/object_info`. It checks model availability but does not run
  generation or prove that weights fit into GPU memory.


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
Rates and increases use counters to tolerate process resets. Generation and
prompt throughput panels show aggregate tokens per second across the selected
models and concurrent requests. They therefore need not match the per-request
throughput printed in an individual llama.cpp completion log. The dashboard also
shows an estimated aggregate rate per currently active request; this is useful
for comparison, but is not exact per-completion timing. The decode-call metric
counts `llama_decode()` calls, not generated tokens. Optional speculative
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
- `LLAMA_CPP_SMALL_THREADS` controls both generation and prompt-processing CPU
  threads. `--batch-size` uses the shared `LLAMA_CPP_BATCH_SIZE` independently.
- All chat workspace presets use `Big`. Existing managed chat presets
  migrate once at startup, tracked by `ai_stack.model_presets.big_chat_v1`,
  preserving custom prompts and settings.
- The Image Generation workspace preset, dedicated router and external task
  setting all use `Small`.

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

## Context compaction (Open WebUI 0.12.0)

The Open WebUI derivative uses Docker Hub base image `openwebui/open-webui:latest` via
`OPENWEBUI_VERSION=latest`. Pin a tested release tag for reproducible upgrades.
Open WebUI 0.12.0 compacts between
native tool rounds as well as before a reply, keeps tool calls with their
results, and provides separate threshold caps and retention controls. Coding,
Allround and Image Generation already use native function calling.

| Setting | Default | Reason |
| --- | --- | --- |
| `ENABLE_CONTEXT_COMPACTION` | `true` | Summarize older history automatically |
| `CONTEXT_COMPACTION_MODEL` | `Big` | Long histories need the main model's context; `Small` has only 16,384 tokens |
| `CONTEXT_COMPACTION_TOKEN_THRESHOLD` | `65536` | 64K trigger for normal chats |
| `CONTEXT_COMPACTION_TOKEN_CAP` | `81920` | 80K ceiling for per-model overrides |
| `CONTEXT_COMPACTION_RETENTION_PERCENTAGE` | `40` | Retain recent messages verbatim, aligned to conversation/tool rounds |
| `OPENWEBUI_SMALL_COMPACTION_THRESHOLD` | `8192` | Earlier trigger for the `Small` base model and Image Generation preset |

The small-model threshold is a stack setting written to Open WebUI's model
parameter `compact_token_threshold`. The cap bounds thresholds, not total prompt
size. Retention is a percentage of **messages**, not tokens. The summary prompt
preserves user constraints, decisions, exact identifiers, tool results and open
work, and distinguishes source-document instructions from the user's request.
Summary output is capped at 2,048 tokens through `TASK_MODEL_PARAMS.max_tokens`;
Open WebUI 0.12.0 shares these parameters with other background tasks, so their
outputs receive the same cap. Normal chat output limits remain unchanged.

The main llama.cpp router keeps `LLAMA_CPP_PARALLEL=4` and a total context pool
of `196608` tokens (192K). Explicit `--kv-unified` lets the usual one or two
active requests share that pool instead of reserving four fixed 49,152-token
partitions. Two 64K chats leave approximately 64K for growth, generation and
summary requests; two chats at the 80K override ceiling leave only 32K. These
are planning margins, not admission guarantees. Compaction itself uses a slot
and can overlap other work. Existing cached prefixes, long responses and large
tool outputs also consume capacity. Four short/medium chats remain possible;
four simultaneous 64K histories do not fit in 192K. For sustained four-way long
chats, lower both threshold and cap to `32768` before the load, or limit active
long chats. Merely having idle slots does not increase GPU memory consumption
by allocating another full 192K pool per slot.

Compaction is not a hard overflow guard: a huge new message or tool result can
overshoot the threshold; very short histories may not have a valid split point.
When changing context sizes or models, review both the summary model's capacity
and every chat model's threshold. Check real latency, context usage and memory
under the intended load before increasing limits.

Tool Search is available as an optional 0.12.0 feature: `ENABLE_TOOL_SEARCH=false`
keeps the experimental feature off initially. For large external/MCP tool sets,
set it to `true`; `TOOL_SEARCH_DEFER_THRESHOLD=1024` defers definitions over 1,024
**characters**, and `TOOL_SEARCH_DEFER_BUILTIN_TOOLS=false` keeps the built-in
terminal, search and image tools immediately available. It requires native
function calling and can introduce an extra tool lookup.

### Applying to existing installations

Deploy the changed Compose file and `config/openwebui/` helpers together. The
installer's deployment manifest includes the new helper. Existing `.env` values
are preserved by the installer: explicitly change `OPENWEBUI_VERSION` to
`latest` to use the Docker Hub default, especially if it still uses the GHCR
`main` tag. Add the new
settings from `.env.example` as needed; Compose includes defaults for them.
Recreate both `llama-cpp` and `openwebui` to apply command/environment changes:

```bash
docker compose build openwebui
docker compose up -d --force-recreate llama-cpp openwebui
docker compose logs openwebui | tail -n 80
```

After the first administrator exists, startup applies the targeted migration
and records `ai_stack.context_compaction.v1` only after read-back verification.
This updates persisted settings even on an existing database, while retaining
model prompts, unrelated parameters and access grants. Later admin changes
survive normal restarts. To deliberately reapply changed environment settings,
recreate Open WebUI and then run:

```bash
docker compose exec -T openwebui python /etc/openwebui/apply_context_compaction.py
docker compose restart openwebui
```

Check Admin Settings > Interface for the compaction settings, the Small/Image
Generation advanced parameters for the 8K threshold, and the llama.cpp logs
for unified KV configuration. Refresh the model list after the initial migration.
A live check should include a long chat, a native tool loop and two concurrent
chats. This repository's local checks do not replace that GPU-backed validation.

Sources: [Open WebUI 0.12.0 release notes](https://github.com/open-webui/open-webui/releases/tag/v0.12.0),
[versioned compaction implementation](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/utils/context_compaction.py),
[versioned configuration keys](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/config.py),
[llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

## Terminal file display

The interface default is **Inline** (German: **Eingebettet**), using Open WebUI
0.12.0's `DEFAULT_INTERFACE_SETTINGS={"terminalFileDisplay":"inline"}`.
The startup helper migrates the saved `ui.default_interface_settings` once,
preserving other interface defaults; explicitly reapplying `apply_open_terminal.py`
also restores this value. Personal interface preferences can override the default.
Deploy/recreate Open WebUI and reload the browser to receive it. An existing user
who explicitly selected Sidebar can change Settings > Interface > Terminal File
Display to Inline, or reset that preference to inherit the administrator default.

## Base-model list cache

`ENABLE_BASE_MODELS_CACHE=true` enables **Cache Base Model List** by default.
The startup helper also writes the persisted Open WebUI 0.12.0 setting
`models.base_models_cache` once for existing installations, verifying the value
before recording `ai_stack.base_models_cache_v1`. Other settings and subsequent
admin changes are preserved. Recreate Open WebUI to apply the deployment change.
This caches the provider's model list, not model weights or chat responses.

## Arena models

`ENABLE_EVALUATION_ARENA_MODELS=false` disables Arena model comparisons by default.
The startup helper also updates the persisted `evaluation.arena.enable` setting
once for existing installations, with read-back verification before recording
`ai_stack.arena_disabled_v1`. Saved Arena model definitions are retained; later
admin changes survive normal restarts. Recreate Open WebUI to apply the change.

## Built-in faster-whisper speech recognition

Open WebUI uses its local Whisper engine (`AUDIO_STT_ENGINE` is an empty string)
with `WHISPER_MODEL=small`, `WHISPER_COMPUTE_TYPE=int8`, automatic language
detection (`WHISPER_LANGUAGE` empty), `WHISPER_VAD_FILTER=true` and
`WHISPER_MODEL_AUTO_UPDATE=true`. Models are stored in
`/app/backend/data/cache/whisper/models`, within the existing persistent
Open WebUI data mount. The standard image runs this on the CPU; no separate
Whisper service or ROCm image is added. Downloads/updates require network access
unless Open WebUI's offline mode suppresses them.

Recreate Open WebUI after deploying the Compose/environment changes. The startup
helper applies the persisted engine and local model once, including to existing
installations, and verifies them before recording `ai_stack.local_whisper_v1`.
The remaining options are environment settings. Later admin engine/model edits
are preserved on restart. Select the Default speech-to-text engine in personal
Audio settings to use the server's local Whisper, then use the chat microphone.

### Topic-dependent search language

The managed model system prompts and automatic query-generation prompt select the
source language per topic: `:en` for technical documentation and international
research, `:de` for Austrian/German local topics, with German as the fallback.
Explicit user language preferences take priority; the answer language is independent.
No additional classifier request is needed.

The search gateway accepts one leading language marker, for example
`:en Python asyncio TaskGroup` or `:de Wien Meldezettel`. It removes the marker
before contacting SearXNG and uses it instead of the incoming `language` parameter.
Other language codes such as `:fr` and `:de-AT` are supported. Repeated or misplaced
markers are rejected. Without a marker, explicit language parameters are preserved;
missing, empty or `all` still means `de`. Cache entries include the effective language.

Recreate/restart **both search-gateway and openwebui** after updating these files.
On startup, a one-time migration appends the guidance to the four managed presets
and the saved query-generation prompt, preserving other settings and existing text.
Later prompt edits are retained. Topic selection depends on model compliance;
the gateway deterministically applies the selected marker or the German fallback.

### Fetched page content limit

`WEB_FETCH_MAX_CONTENT_LENGTH=50000` limits `fetch_url` output to 50,000 extracted
characters per page, not tokens. Longer content is truncated rather than summarized.
Compose defaults to this value even when an existing `.env` omits the variable.
Recreate Open WebUI to apply the change. The startup helper also updates the
persisted `web.fetch.max_content_length` once for existing installations, verifies
it, and records `ai_stack.web_fetch_content_limit_v1`. Later admin edits survive
normal restarts.

## Remote Playwright web loader

`fetch_url`, URL retrieval and search-result page loading use the upstream
Playwright loader. SearXNG and search-gateway still own search queries.
The existing 50,000-character fetch limit and loader concurrency remain unchanged.

There is no manually configured Playwright version. On each explicit install or
update, `scripts/prepare_playwright.py` inspects the freshly built WebUI image by
image ID in a read-only, networkless container without deployment mounts. It reads
installed `playwright`/`unstructured` package versions and WebUI metadata (including
`/app/package.json` for source-based official images), then pulls the official
Linux amd64 `mcr.microsoft.com/playwright/python:v<detected>-noble` image.
Missing packages, prerelease Playwright versions, unsupported architecture or
unavailable images stop installation; no newer version or distribution is substituted.
The current helpers require WebUI >= 0.12.0 for its config API and loader protections.

`compose.playwright.json` contains only generated browser build arguments: the
exact detected package version and the pulled upstream image's registry digest.
`playwright-inventory.json` records the source WebUI image ID and detected versions.
Neither file contains credentials; both are ignored by Git. The browser Dockerfile
installs the matching package during build and uses Playwright's bundled CLI
`launch-server` with `chromiumSandbox: true` and the full `chromium` headless channel
(which provides `chrome://sandbox`). No custom Node/npm code or downloads are
introduced at runtime. Chromium and its libraries live in the browser image;
WebUI retains its upstream client/extraction packages and Pandoc derivative.

The installer includes the generated override for builds and image inventory.
Ordinary systemd startup does not need it: runtime settings are entirely in base
Compose, and `compose.image-lock.json` selects the previously verified image IDs.
Updating WebUI alone does not update an already running browser; always run the
paired preparation/build/check sequence after an explicit WebUI rebuild.

The browser uses the internal `browser-net`, shared only with WebUI and web-egress.
It has no external route, host port, host IPC, data volume, credentials or GPU.
Only web-egress also joins the dedicated external `browser-outbound` network.
The proxy resolves each target once, rejects any non-public DNS answer (including
mixed answers and IPv6 transition addresses), and connects to the validated numeric
IP without a second DNS lookup. HTTP is restricted to port 80 and CONNECT to 443.
Every redirected HTTP request or new HTTPS tunnel is checked again. The proxy
allows 32 concurrent connections, 30-second idle timeouts, 120-second transfer
lifetimes, 4 MiB HTTP request bodies and 64 MiB transfers. It does not log URLs.
These bounds can prevent unusually large/slow pages from loading.

Recent upstream loaders intercept browser requests and issue HTTP requests in the
WebUI process. Those requests retain upstream SSRF/redirect validation with local
web fetch disabled and SSL verification enabled. WebUI remains a trusted service
with its existing networks; browser network isolation is not a firewall around
WebUI or its model/terminal integrations. No global HTTP proxy is configured in
WebUI, avoiding changes to model and Whisper downloads. `WEB_SEARCH_TRUST_ENV=false`
makes the loader ignore ambient proxy variables.

The one-time `ai_stack.remote_playwright_v1` migration writes and verifies
`web.loader.engine`, `web.loader.playwright_ws_url`,
`web.loader.playwright_timeout`, `web.loader.ssl_verification` and
`web.search.trust_env`. Later admin edits survive restarts. The installer explicitly
runs `--browser-loader-only` before its final WebUI restart to clear cached settings.
For manual deployment, run the same migration and restart WebUI after recreation.

Runtime defaults are `PLAYWRIGHT_TIMEOUT=30000` milliseconds, 2 CPUs, 2 GiB memory,
256 processes and 512 MiB shared memory. The browser runs as `pwuser`, with dropped
capabilities, no-new-privileges, read-only root, private shared memory and a bounded
512 MiB `/tmp` tmpfs. Host kernel/AppArmor policy must permit Chromium's unprivileged
user namespaces. Health checks require both namespace and Seccomp-BPF sandboxes
reported by `chrome://sandbox`; failure stops readiness rather than disabling them.

`config/playwright/seccomp.json` is based on the
[Moby default profile](https://github.com/moby/profiles/blob/main/seccomp/default.json)
with the `clone`, `setns`, `unshare` allowance recommended by
[Playwright](https://playwright.dev/python/docs/docker#crawling-and-scraping).
It preserves the modern `clone3` ENOSYS fallback for libc. Review this checked-in
policy on host/browser upgrades; it is not downloaded automatically at startup.

`scripts/verify_playwright.py` starts only the browser and proxy in a uniquely named,
disposable Compose project. It retains their production restrictions and health
checks and runs a client from the actual WebUI image without credentials or data
mounts. It checks JavaScript, blocked internal HTTP/HTTPS destinations, browser
proxy use and a public HTTPS fetch, then removes that project's containers/networks.
This runs before replacing the image lock, including under `--no-start`. It requires
Docker, permitted user namespaces and Internet access to `https://example.com/`.
A full chat invoking `fetch_url` still needs verification on the deployed WebUI.

## Security defaults

`apply_security.py` synchronizes managed provider credentials, creates the empty
`OPEN_TERMINAL_ACCESS_GROUP` (default `Terminal-Zugriff`) and enforces its terminal
connection grant. Registration closes after the first administrator exists when
`CLOSE_SIGNUP_AFTER_BOOTSTRAP=true`. New security migrations use a 4-hour JWT expiry;
later admin expiry edits persist. See [SECURITY.md](SECURITY.md) for full behavior.

NGINX renders `security.conf.template` into a temporary writable directory using
only `PUBLIC_ORIGIN` and `COMFYUI_ORIGIN`. This keeps NGINX variables literal while
binding browser origins to the configured public hostname and port. ComfyUI uses
a bcrypt password file generated by the installer. The certificate-directory
permissions are intentionally unchanged.

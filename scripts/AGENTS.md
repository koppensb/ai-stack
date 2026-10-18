# Installation and helper instructions

The root `AGENTS.md` applies here. Read `INSTALL.md` and the relevant sections of
`CONFIGURATION.md` before changing installation or update behavior.

## Responsibilities

- `configure_install.py` prepares deployment settings and generated overrides.
- `security_settings.py` centralizes credential validation and authentication files.
- `install_gpu_driver.sh` handles optional supported-host driver preparation.
- `deployment_files.txt` is the runtime/build file allowlist;
  `check_managed_files.py` detects differences in existing managed target files.
- `lock_images.py` records installed image identities.
- `download_models.py` plans and downloads router and image-model assets.
- `check_amd_runtime.py`, `verify_gpu_compute.py`, and `verify_comfyui_gpu.py`
  check different aspects of the installed runtime; they are not generic unit tests.

## Change rules

- Keep host orchestration in Bash and structured configuration logic in Python.
  Retain strict Bash error handling, quoted paths, and explicit exit statuses.
- Trace helper inputs and invocation in `install_ai_stack.sh`. Some helpers rely on
  environment variables or deployment paths and execute at module scope. Do not
  assume every helper has a safe `--help` mode or is safe to import.
- Preserve locking, backup behavior, rerun safety, and the distinction between
  missing files and customized existing files. Managed-file replacement uses
  `--update-files`; do not silently overwrite operator customizations.
- `deployment_files.txt` contains exactly one relative file path per line, with
  no comments or blank lines. Add entries only for required build/runtime files.
  Verify new entries exist and that their runtime mounts and callers agree.
- Preserve existing credentials and encryption keys. Generate only where the
  existing initialization/migration rules permit it. Reject ambiguous mismatches;
  do not resolve them by silently choosing or rotating a key.
- Keep GPU support checks explicit. Driver preparation does not automatically
  upgrade/downgrade a working mismatched driver or reboot the host. Preserve exit
  codes 20 (reboot needed) and 21 (manual Secure Boot enrollment needed).
- `--no-start` still configures, builds, and downloads; it is not a dry run.
  Use the downloader's `--plan` only for its documented planning scope.
- Keep partial-download resume and completed-file reuse behavior explicit. Do not
  delete old models as an incidental part of changing presets.
- For changed installer logic, verify fresh-install, rerun, failure, and existing
  configuration paths with isolated fixtures or mocks where practical. Document
  hardware/deployment checks that cannot be performed locally.

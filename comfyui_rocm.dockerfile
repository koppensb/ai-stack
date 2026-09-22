# syntax=docker/dockerfile:1.7

ARG ROCM_PYTORCH_IMAGE=rocm/pytorch:rocm10.0_ubuntu26.04_py3.14_pytorch_release_2.13.0
FROM ${ROCM_PYTORCH_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
# latest is resolved to the newest stable GitHub release during the build.
# Compose disables cache reuse for this image; restarting a container does not
# fetch source updates. A direct Docker build may explicitly select another ref.
ARG COMFYUI_REF=latest
ARG COMFYUI_GGUF_REF=main

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTORCH_ALLOC_CONF=expandable_segments:True

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# The AMD base image already provides the matching ROCm builds of torch,
# torchvision and torchaudio. A normal requirements install keeps them instead
# of replacing them with incompatible wheels from PyPI.
RUN git clone --filter=blob:none https://github.com/Comfy-Org/ComfyUI.git \
        /opt/ComfyUI \
    && cd /opt/ComfyUI \
    && if [ "${COMFYUI_REF}" = latest ]; then \
        COMFYUI_REF="$(curl --fail --silent --show-error --location --retry 3 \
            https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest \
            | python -c 'import json, sys; tag = json.load(sys.stdin)["tag_name"]; assert isinstance(tag, str) and tag; print(tag)')"; \
       fi \
    && git checkout --detach "${COMFYUI_REF}" \
    && printf 'ComfyUI release: %s\n' "${COMFYUI_REF}" \
    && python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt \
    && python -c "import torch; assert torch.version.hip, 'ROCm-enabled PyTorch is required'; print('PyTorch', torch.__version__, 'ROCm', torch.version.hip)"

# The leejet fork supports the qwen_image21 GGUF architecture.
# Keep bundled nodes outside the bind-mounted custom_nodes directory.
# Dependencies are installed at build time: backend-net has no Internet access.
RUN git clone --filter=blob:none https://github.com/leejet/ComfyUI-GGUF.git \
        /opt/comfyui-bundled-nodes/ComfyUI-GGUF \
    && cd /opt/comfyui-bundled-nodes/ComfyUI-GGUF \
    && git checkout --detach "${COMFYUI_GGUF_REF}" \
    && python -c "from pathlib import Path; assert 'qwen_image21' in Path('loader.py').read_text(), 'Qwen Image 2.1 GGUF support required'" \
    && python -m pip install -r requirements.txt \
    && python -c "import gguf, torch; assert torch.version.hip, 'ROCm-enabled PyTorch is required'" \
    && printf 'bundled_nodes:\n  custom_nodes: /opt/comfyui-bundled-nodes\n' \
        > /opt/ComfyUI/extra_model_paths.yaml

WORKDIR /opt/ComfyUI

# Open WebUI resumes chat after the worker publishes completion. Release GPU
# allocations synchronously before that boundary, not via asynchronous /free.
# Keep this build-time patch guarded: an upstream worker change needs review.
RUN python - <<'PY_HANDOFF'
import ast
from pathlib import Path

path = Path("main.py")
source = path.read_text()
anchor = "            q.task_done(item_id,\n"
if source.count(anchor) != 1:
    raise SystemExit("Unsupported ComfyUI worker: VRAM handoff anchor changed")
# Preserve execution results around e.reset(): clients still need the original
# history and success status after model/allocator cleanup completes.
cleanup = '''            # AI_STACK_VRAM_HANDOFF: the queue stays busy until release finishes.
            handoff_started = time.perf_counter()
            handoff_history = e.history_result
            handoff_success = e.success
            handoff_messages = e.status_messages
            try:
                comfy.model_management.unload_all_models()
                e.reset()
                gc.collect()
                comfy.model_management.soft_empty_cache(force=True)
                logging.info("AI stack: ComfyUI VRAM released before completion (%.2fs)",
                             time.perf_counter() - handoff_started)
            except Exception:
                # Keep the image/history and worker alive; the coordinator retries.
                logging.exception("AI stack: ComfyUI VRAM handoff failed; coordinator cleanup required")
            finally:
                e.history_result = handoff_history
                e.success = handoff_success
                e.status_messages = handoff_messages
'''
source = source.replace(anchor, cleanup + anchor)
ast.parse(source)
path.write_text(source)
PY_HANDOFF

RUN python -c "from pathlib import Path; assert 'class TextEncodeQwenImage21' in Path('comfy_extras/nodes_qwen.py').read_text(), 'Update ComfyUI for Qwen Image 2.1 support'"

RUN mkdir -p input output user models custom_nodes

VOLUME ["/opt/ComfyUI/input", "/opt/ComfyUI/output", "/opt/ComfyUI/user", "/opt/ComfyUI/models", "/opt/ComfyUI/custom_nodes"]

EXPOSE 8188

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=10 \
    CMD curl -fsS http://127.0.0.1:8188/system_stats >/dev/null || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--listen", "0.0.0.0", "--port", "8188"]

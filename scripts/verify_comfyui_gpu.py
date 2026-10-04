"""Fail unless the ComfyUI container can access a GPU through PyTorch."""

import torch


if not torch.cuda.is_available():
    raise SystemExit("ComfyUI cannot detect a GPU")

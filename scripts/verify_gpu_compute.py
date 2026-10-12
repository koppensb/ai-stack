"""Exercise the built ComfyUI ROCm runtime before downloading model weights."""
import torch

if not torch.version.hip or not torch.cuda.is_available():
    raise SystemExit("The ComfyUI ROCm runtime cannot access an AMD GPU")
# A small allocation and real kernel launch detect more than device enumeration.
for index in range(torch.cuda.device_count()):
    device = f"cuda:{index}"
    x = torch.ones((32, 32), device=device)
    result = x @ x
    torch.cuda.synchronize(index)
    if not torch.all(result == 32).item():
        raise SystemExit(f"GPU computation failed on {device}")
    print(f"GPU compute OK: {torch.cuda.get_device_name(index)} (HIP {torch.version.hip})")
    del x, result

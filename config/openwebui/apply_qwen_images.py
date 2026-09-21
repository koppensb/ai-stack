"""Apply Qwen image, prompt-task and managed connection settings to Open WebUI.

Run inside the current Open WebUI container, then restart it to clear caches.
"""
import asyncio
import json
import os
import sys
from start import load_workflows, IMAGE_PROMPT_TEMPLATE


# These per-key writes replace the managed image configuration on every run.
# Database persistence means changing only Compose/.env may not change the UI.
def settings():
    workflows = load_workflows()
    return {
        # The external task model also serves tasks such as title/tag generation;
        # this is an API model ID, not the selectable workspace preset name.
        "task.model.external": "Qwen3.5-4B",
        "task.image.prompt_template": IMAGE_PROMPT_TEMPLATE,
        "image_generation.enable": os.environ.get("ENABLE_IMAGE_GENERATION", "true").lower() == "true",
        "image_generation.prompt.enable": os.environ.get("ENABLE_IMAGE_PROMPT_GENERATION", "true").lower() == "true",
        "image_generation.engine": "comfyui",
        "image_generation.model": os.environ.get("IMAGE_GENERATION_MODEL", "qwen-image-2512-Q4_K_M.gguf"),
        "image_generation.size": os.environ.get("IMAGE_SIZE", "1024x1024"),
        "image_generation.steps": int(os.environ.get("IMAGE_STEPS", "40")),
        "image_generation.comfyui.base_url": "http://comfyui:8188",
        "image_generation.comfyui.workflow": workflows["COMFYUI_WORKFLOW"],
        "image_generation.comfyui.nodes": json.loads(workflows["COMFYUI_WORKFLOW_NODES"]),
        "images.edit.enable": os.environ.get("ENABLE_IMAGE_EDIT", "true").lower() == "true",
        "images.edit.engine": "comfyui",
        "images.edit.model": os.environ.get("IMAGE_EDIT_MODEL", "qwen-image-edit-2511-Q4_K_M.gguf"),
        "images.edit.size": os.environ.get("IMAGE_EDIT_SIZE", "1024x1024"),
        "images.edit.comfyui.base_url": "http://comfyui:8188",
        "images.edit.comfyui.workflow": workflows["IMAGES_EDIT_COMFYUI_WORKFLOW"],
        "images.edit.comfyui.nodes": json.loads(workflows["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"]),
    }


async def connection_settings(config):
    # Append managed endpoints without discarding unrelated saved connections.
    urls = list(await config.get("openai.api_base_urls") or [])
    # Preserve positional alignment: keys[i] authenticates urls[i], including
    # pre-existing endpoints that are unrelated to this stack.
    keys = list(await config.get("openai.api_keys") or [])[:len(urls)]
    keys.extend([""] * max(0, len(urls) - len(keys)))
    managed_urls = os.environ["OPENAI_API_BASE_URLS"].split(";")
    managed_keys = os.environ["OPENAI_API_KEYS"].split(";")
    for url, key in zip(managed_urls, managed_keys, strict=True):
        if url in urls:
            keys[urls.index(url)] = key
        else:
            urls.append(url)
            keys.append(key)
    return {"openai.api_base_urls": urls, "openai.api_keys": keys}


async def main():
    sys.path.insert(0, "/app/backend")
    try:
        from open_webui.models.config import Config
    except ImportError as error:
        raise RuntimeError("Update Open WebUI or import the Qwen workflows in Admin Settings > Images; this build lacks the per-key config API.") from error
    await Config.upsert({**settings(), **await connection_settings(Config)})
    print("Qwen generation and editing settings saved. Restart Open WebUI and refresh the browser.")


if __name__ == "__main__":
    asyncio.run(main())

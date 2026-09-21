#!/usr/bin/env python3
"""Prefetch every llama.cpp preset and the configured ComfyUI model assets.

Uses `llama download` in the built image, sharing the router's LLAMA_CACHE.
No inference or GPU model loading is performed. Credentials stay in Compose.
"""
import argparse
import configparser
import json
import os
from pathlib import Path
import subprocess


# Keep the vision tower prefix aligned with the text encoder for GGUF discovery.
IMAGE_ASSETS = (
    ('unet/qwen-image-2512-Q4_K_M.gguf',
     'https://huggingface.co/unsloth/Qwen-Image-2512-GGUF/resolve/main/qwen-image-2512-Q4_K_M.gguf'),
    ('unet/qwen-image-edit-2511-Q4_K_M.gguf',
     'https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF/resolve/main/qwen-image-edit-2511-Q4_K_M.gguf'),
    ('text_encoders/Qwen2.5-VL-7B-Instruct-UD-Q4_K_XL.gguf',
     'https://huggingface.co/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/Qwen2.5-VL-7B-Instruct-UD-Q4_K_XL.gguf'),
    ('text_encoders/Qwen2.5-VL-7B-Instruct-mmproj-BF16.gguf',
     'https://huggingface.co/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/mmproj-BF16.gguf'),
    ('vae/qwen_image_vae.safetensors',
     'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors'),
)


def download_image_models(directory):
    directory = Path(directory)
    for relative, url in IMAGE_ASSETS:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Nonempty completed files are trusted, not checksum-verified. Delete a
        # corrupt completed file explicitly; .part files resume after interruption.
        if target.is_file() and target.stat().st_size:
            print('Already present: ' + relative, flush=True)
            continue
        print('Downloading: ' + relative, flush=True)
        partial = target.with_name(target.name + '.part')
        subprocess.run(['curl', '--fail', '--location', '--retry', '5',
                        '--continue-at', '-', '--output', str(partial), url], check=True)
        # Publish the final filename only after curl reports successful completion.
        partial.replace(target)


def flag(value):
    return str(value).strip().lower() in {'true', '1', 'yes', 'on'}


def chat_plan(path):
    parser = configparser.ConfigParser(interpolation=None, delimiters=('=',), strict=True)
    with open(path) as handle:
        parser.read_file(handle)
    # The router uses [*] as shared defaults; ConfigParser does not merge it itself.
    defaults = dict(parser['*']) if parser.has_section('*') else {}
    plan = []
    for name in parser.sections():
        if name == '*':
            continue
        options = {**defaults, **dict(parser[name])}
        repo = options.get('hf') or options.get('hf-repo')
        model = options.get('model')
        url = options.get('model-url')
        if repo:
            args = ['--hf-repo', repo]
            if options.get('hf-file'):
                args += ['--hf-file', options['hf-file']]
        elif url:
            args = ['--model-url', url]
            if model:
                args += ['--model', model]
        elif model:
            args = ['--model', model]
        else:
            raise ValueError(f'Preset {name!r} needs hf, hf-repo, model-url or model; refusing to skip it.')
        # Ask llama download to include MTP assets required by the inference preset.
        if 'draft-mtp' in options.get('spec-type', '').split(','):
            args += ['--mtp']
        if flag(options.get('no-mmproj', False)) or options.get('mmproj-auto', '').lower() == 'false':
            args += ['--no-mmproj']
        plan.append((name, args))
        draft_repo = options.get('spec-draft-hf') or options.get('hf-repo-draft')
        draft_model = options.get('spec-draft-model') or options.get('model-draft')
        if draft_repo:
            draft = ['--hf-repo', draft_repo]
            if draft_model:
                draft += ['--hf-file', draft_model]
            plan.append((name + ' (draft)', draft))
        elif draft_model:
            plan.append((name + ' (local draft)', ['--model', draft_model]))
    if not plan:
        raise ValueError('No chat models are defined in models.ini.')
    return plan


def compose_command(root):
    command = ['docker', 'compose', '--project-name', 'ai-stack', '--env-file', str(root/'.env'),
               '-f', str(root/'docker-compose.yml')]
    if (root/'compose.install.yml').exists():
        command += ['-f', str(root/'compose.install.yml')]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--plan', action='store_true', help='Show the model plan without Docker or downloads')
    args = parser.parse_args()
    root = args.root.resolve()
    plan = [(service, name, flags)
            for service in ('llama-cpp', 'llama-cpp-image')
            for name, flags in chat_plan(root/f'config/{service}/models.ini')]
    for service, name, _ in plan:
        print('Chat model: ' + name, flush=True)
    print('Image assets: Qwen generation, editing, text encoder, vision tower and VAE', flush=True)
    if args.plan:
        return
    command = compose_command(root)
    # Avoid exported host variables silently overriding the deployment .env.
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/root')}
    # Resolve configuration privately, using exactly the same interpolation as Compose.
    config = json.loads(subprocess.check_output(command + ['config', '--format', 'json'], env=env))
    image_env = config['services']['openwebui']['environment']
    expected = {'IMAGE_GENERATION_MODEL': 'qwen-image-2512-Q4_K_M.gguf',
                'IMAGE_EDIT_MODEL': 'qwen-image-edit-2511-Q4_K_M.gguf'}
    for key, value in expected.items():
        if image_env.get(key) != value:
            raise ValueError(f'{key} does not match the bundled Qwen workflows; update .env and the download manifest together.')
    # Prefetch into the same bind mount/cache that llama-server uses at runtime.
    # --no-deps prevents startup of the stack; no service ports are published.
    for service, name, download_args in plan:
        print('Downloading/checking ' + name, flush=True)
        subprocess.run(command + ['run', '--rm', '--no-deps', '-T', '--entrypoint', 'llama',
                                  service, 'download', *download_args], env=env, check=True)
    volumes = config['services']['comfyui']['volumes']
    models = next(v['source'] for v in volumes if v['target'] == '/opt/ComfyUI/models')
    download_image_models(models)
    print('All configured model downloads completed. Models enter VRAM only when requested.')


if __name__ == '__main__':
    main()

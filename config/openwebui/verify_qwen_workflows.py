"""Validate workflows against the running ComfyUI node schemas (no inference)."""
import json
import os
from urllib.request import urlopen
from start import load_workflows


def verify(workflows, info):
    errors = []
    for prefix, encoded in workflows.items():
        if not prefix.endswith('_WORKFLOW'):
            continue
        workflow = json.loads(encoded)
        for node_id, node in workflow.items():
            kind = node['class_type']
            if kind not in info:
                errors.append(f'{prefix}/{node_id}: missing node {kind}')
                continue
            schema = info[kind].get('input', {})
            inputs = node['inputs']
            required = schema.get('required', {})
            allowed = {**required, **schema.get('optional', {})}
            for key in required:
                if key not in inputs:
                    errors.append(f'{prefix}/{node_id}: missing input {key}')
            for key, value in inputs.items():
                if key not in allowed:
                    errors.append(f'{prefix}/{node_id}: unknown input {key}')
                    continue
                expected = allowed[key][0]
                # API-graph links are [source_node_id, output_index]; enum values
                # are checked against the running node schema when they are literals.
                if isinstance(value, list):
                    source = workflow.get(str(value[0]))
                    outputs = info.get(source['class_type'], {}).get('output', []) if source else []
                    if len(value) != 2 or not isinstance(value[1], int) or not 0 <= value[1] < len(outputs):
                        errors.append(f'{prefix}/{node_id}: invalid link for {key}')
                    elif isinstance(expected, str) and expected != '*' and outputs[value[1]] != expected:
                        errors.append(f'{prefix}/{node_id}: type mismatch for {key}')
                # The image filename is an upload placeholder until request time;
                # model filenames, by contrast, must already be available.
                elif isinstance(expected, list) and key != 'image' and value not in expected:
                    errors.append(f'{prefix}/{node_id}: unavailable {key}={value}')
    # A missing vision tower can silently break editing despite a valid prompt.
    clips = info.get('CLIPLoaderGGUF', {}).get('input', {}).get('required', {}).get('clip_name', [[]])[0]
    if 'Qwen2.5-VL-7B-Instruct-mmproj-BF16.gguf' not in clips:
        errors.append('Missing Qwen2.5-VL-7B-Instruct-mmproj-BF16.gguf vision tower')
    return errors


def main():
    url = os.environ.get('COMFYUI_BASE_URL', 'http://comfyui:8188').rstrip('/')
    with urlopen(url + '/object_info', timeout=30) as response:
        info = json.load(response)
    errors = verify(load_workflows(), info)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Qwen node schemas, model filenames and graph connections verified. Run a generation and an edit to verify inference.')


if __name__ == '__main__':
    main()

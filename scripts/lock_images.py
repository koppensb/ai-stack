"""Record installed image identities for reproducible startup, without exporting secrets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/ai-stack'))
    args = parser.parse_args()
    root = args.root.resolve()
    command = ['docker', 'compose', '--project-name', 'ai-stack', '--env-file', str(root/'.env'),
               '-f', str(root/'docker-compose.yml')]
    if (root/'compose.install.yml').exists():
        command += ['-f', str(root/'compose.install.yml')]
    if (root/'compose.playwright.json').exists():
        command += ['-f', str(root/'compose.playwright.json')]
    # Match installer precedence; ambient variables must not override deployed .env.
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/root')}
    config = json.loads(subprocess.check_output(command + ['config', '--format', 'json'], env=env))
    services, inventory = {}, {}
    for name, service in config['services'].items():
        reference = service['image']
        info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference], env=env))[0]
        # Local image IDs also pin custom ROCm builds without requiring a registry.
        identity = info['Id']
        if not identity.startswith('sha256:'):
            raise RuntimeError(f'No immutable image identity for {name}')
        services[name] = {'image': identity, 'pull_policy': 'never'}
        inventory[name] = {'requested_image': reference, 'image_id': identity,
                           'repo_digests': info.get('RepoDigests') or []}
    for filename, data in [('compose.image-lock.json', {'services': services}),
                           ('image-inventory.json', inventory)]:
        fd, temporary = tempfile.mkstemp(prefix='.image-lock-', dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out, indent=2)
                out.write('\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, root/filename)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print('Installed image identities locked; inventory contains no environment or credentials.')


if __name__ == '__main__':
    main()

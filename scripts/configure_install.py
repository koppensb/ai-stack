import datetime, getpass, ipaddress, os, pathlib, re, secrets, shutil, subprocess, warnings
root = pathlib.Path('/opt/ai-stack')
source = pathlib.Path(os.environ['AI_INSTALL_SOURCE'])
path = root / '.env'
original = path.read_text()
# A minimal Compose document resolves .env syntax without evaluating required
# service variables before empty/example credentials have been generated.
result = subprocess.run(['docker', 'compose', '--env-file', str(source/'.env.example'), '--env-file', str(path), '-f', '-', 'config', '--environment'], input='services:\n  bootstrap:\n    image: busybox\n', env={'PATH': os.environ['PATH'], 'HOME': '/root'}, text=True, stdout=subprocess.PIPE, check=True).stdout
values = dict(line.split('=', 1) for line in result.splitlines() if '=' in line)
host = os.environ['AI_INSTALL_HOST'] or values.get('PUBLIC_IP_OR_DOMAIN', '')
if not host or host == 'localhost':
    raise SystemExit('Specify --host with the DNS name or IPv4 address of the Ubuntu machine.')
try:
    addr = ipaddress.ip_address(host)
    if addr.version != 4: raise SystemExit('Use an IPv4 address or DNS name for --host.')
    san = 'IP:' + host
except ValueError:
    if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p) for p in host.split('.')):
        raise SystemExit('Invalid DNS name for --host.')
    san = 'DNS:' + host
def prompt_secret(name, default, empty_action):
    if os.environ.get('AI_INSTALL_NON_INTERACTIVE') == '1':
        return default
    try:
        with open('/dev/tty', 'w') as terminal:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                entered = getpass.getpass(f'{name} (hidden; Enter to {empty_action}): ', stream=terminal)
    except (OSError, EOFError, getpass.GetPassWarning):
        raise SystemExit('A terminal is required for secret prompts. Use --non-interactive for unattended installation.')
    return entered or default

secret = root/'config/llama-cpp/api-key.txt'
old_key = secret.read_text().strip() if secret.exists() else ''
saved_key = values.get('LLAMA_CPP_API_KEY', '')
if saved_key in ('', 'sk-llm-inference-stack-super-secret-key-12345'):
    saved_key = old_key
if old_key and saved_key != old_key:
    raise SystemExit('.env and api-key.txt contain different keys; synchronize them first.')
saved_password = values.get('GRAFANA_ADMIN_PASSWORD', '')
if saved_password in ('', 'admin', 'changeme'):
    saved_password = ''
has_grafana_db = (root/'data/grafana/grafana.db').exists()
if has_grafana_db:
    print('Grafana already has a database. Enter its current admin password; this installer does not reset it.')
password = prompt_secret('GRAFANA_ADMIN_PASSWORD', saved_password,
                         'keep the saved value' if saved_password else ('leave unset' if has_grafana_db else 'generate a password'))
if has_grafana_db and not password:
    raise SystemExit('Existing Grafana database: enter the current password in .env first.')
password = password or secrets.token_hex(24)
hf_token = values.get('HF_TOKEN', '')
if hf_token == 'hf_your_token_here':
    hf_token = ''
hf_token = prompt_secret('HF_TOKEN', hf_token, 'keep the saved value' if hf_token else 'skip (optional)')
# Generate once without prompting; retain the synchronized deployment key.
key = saved_key or secrets.token_hex(32)
# Backfill newly introduced deployment variables without overwriting saved values.
# Compose parses both files, so quoted values and precedence follow Compose rules.
template_names = re.findall(r'^([A-Z][A-Z0-9_]*)=', (source/'.env.example').read_text(), re.M)
present_names = set(re.findall(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=', original, re.M))
updates = {name: values[name] for name in template_names if name not in present_names}
# Preserve existing router tuning while renaming its deployment variables.
for new_name, old_name in (
    ('LLAMA_CPP_SMALL_CTX_SIZE', 'LLAMA_CPP_IMAGE_CTX_SIZE'),
    ('LLAMA_CPP_SMALL_THREADS', 'LLAMA_CPP_IMAGE_THREADS'),
):
    if new_name not in present_names and old_name in values:
        updates[new_name] = values[old_name]
updates.update({'AI_STACK_ROOT': str(root), 'PUBLIC_IP_OR_DOMAIN': host,
           'LLAMA_CPP_API_KEY': key, 'GRAFANA_ADMIN_PASSWORD': password,
           'ROCM_GFX_TARGETS': os.environ['AI_INSTALL_GFX'],
           'LLAMA_CPP_REF': os.environ['AI_INSTALL_REF'] or values['LLAMA_CPP_REF']})
# Enable prompt generation when introducing the dedicated image-prompt service.
# Later installer runs preserve an intentional disable in the deployed .env.
if not {'LLAMA_CPP_SMALL_CTX_SIZE', 'LLAMA_CPP_IMAGE_CTX_SIZE'} & present_names:
    updates['ENABLE_IMAGE_PROMPT_GENERATION'] = 'true'
# Migrate only the exact obsolete bundled image defaults; preserve custom values.
if values.get('IMAGE_GENERATION_MODEL') == 'flux2-dev-Q4_K_M.gguf':
    updates['IMAGE_GENERATION_MODEL'] = 'qwen-image-2.1-Q4_K_M.gguf'
    if values.get('IMAGE_SIZE') == '1104x1472':
        updates['IMAGE_SIZE'] = '1024x1024'
# Upgrade the bundled generation/editing pair to the unified image model.
for image_key, previous in (
    ('IMAGE_GENERATION_MODEL', 'qwen-image-2512-Q4_K_M.gguf'),
    ('IMAGE_EDIT_MODEL', 'qwen-image-edit-2511-Q4_K_M.gguf'),
):
    if values.get(image_key) == previous:
        updates[image_key] = 'qwen-image-2.1-Q4_K_M.gguf'
updates['HF_TOKEN'] = hf_token
search_secret = values.get('SEARXNG_SECRET', '')
updates['SEARXNG_SECRET'] = (secrets.token_hex(32)
                            if search_secret in ('', 'ultrasecretkey') else search_secret)
terminal_key = values.get('OPEN_TERMINAL_API_KEY', '')
updates['OPEN_TERMINAL_API_KEY'] = terminal_key or secrets.token_hex(32)
webui_secret = values.get('WEBUI_SECRET_KEY', '')
updates['WEBUI_SECRET_KEY'] = webui_secret or secrets.token_hex(32)
# Values are written as single-quoted Compose literals, not shell code. Reject
# unsupported characters before writing so credentials cannot alter .env syntax.
for name, value in updates.items():
    if any(c in value for c in "\n\r'"):
        raise SystemExit(f'{name}: the installer does not support line breaks or single quotes.')
# Keep comments and unknown keys; collapse duplicate assignments for managed keys.
lines, seen = [], set()
for line in original.splitlines():
    name = next((name for name in updates if re.match(r'^\s*(?:export\s+)?'+name+r'\s*=', line)), None)
    if name is None:
        lines.append(line)
    elif name not in seen:
        lines.append(f"{name}='{updates[name]}'")
        seen.add(name)
lines.extend(f"{k}='{v}'" for k,v in updates.items() if k not in seen)
updated = '\n'.join(lines) + '\n'
# Retain this marker across interrupted installs until consumers are recreated.
if old_key and old_key != key:
    (root/'.installer-recreate-required').touch(mode=0o600)
if updated != original:
    backup = path.with_name('.env.backup-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S%f'))
    shutil.copyfile(path, backup); backup.chmod(0o600)
    temporary = path.with_name('.env.install-tmp')
    temporary.touch(mode=0o600); temporary.write_text(updated); temporary.chmod(0o600)
    temporary.replace(path)
path.chmod(0o600)
if not secret.exists() or old_key != key:
    secret.touch(mode=0o600); secret.chmod(0o600); secret.write_text(key + '\n')
# Preserve an existing certificate pair; changing --host does not renew it.
certdir = root/'config/nginx/certs'
certdir.mkdir(parents=True, exist_ok=True)
certdir.chmod(0o1777)
crt, certkey = certdir/'nginx.crt', certdir/'nginx.key'
if crt.exists() != certkey.exists(): raise SystemExit('Incomplete TLS certificate pair; fix this first.')
if not crt.exists():
    subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:4096', '-days', '365', '-keyout', str(certkey), '-out', str(crt), '-subj', '/CN='+host, '-addext', 'subjectAltName='+san+',DNS:localhost,IP:127.0.0.1'], check=True)
certkey.chmod(0o600); crt.chmod(0o644)

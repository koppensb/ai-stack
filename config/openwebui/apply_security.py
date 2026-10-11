"""Apply stack security policy and synchronize managed credentials at startup."""
import os

GROUP_MARKER = "ai-stack-terminal-access-v1"
SECURITY_KEY = "ai_stack.security_defaults_v1"


async def terminal_grants(groups, group_form, owner_id):
    name = os.environ.get("OPEN_TERMINAL_ACCESS_GROUP", "Terminal-Zugriff").strip()
    if not name:
        raise ValueError("OPEN_TERMINAL_ACCESS_GROUP must not be empty")
    group = await groups.get_group_by_name(name)
    if group is not None and (group.data or {}).get("ai_stack_managed_by") != GROUP_MARKER:
        raise RuntimeError("Terminal group name is already in use; choose a distinct OPEN_TERMINAL_ACCESS_GROUP")
    if group is None:
        group = await groups.insert_new_group(owner_id, group_form(
            name=name,
            description="Members may execute commands in the shared AI Stack terminal. Add only trusted users.",
            permissions={}, data={"ai_stack_managed_by": GROUP_MARKER, "config": {"share": False}},
        ))
    if group is None:
        raise RuntimeError("Failed to create terminal access group")
    return [{"principal_type": "group", "principal_id": group.id, "permission": "read"}]


async def apply_security(config, users, groups, group_form):
    # Every startup refreshes only keys for these exact managed provider URLs.
    urls = list(await config.get("openai.api_base_urls") or [])
    keys = list(await config.get("openai.api_keys") or [])[:len(urls)]
    keys.extend([""] * max(0, len(urls) - len(keys)))
    managed_urls = os.environ["OPENAI_API_BASE_URLS"].split(";")
    managed_keys = os.environ["OPENAI_API_KEYS"].split(";")
    for url, key in zip(managed_urls, managed_keys, strict=True):
        if url not in ("http://llama-cpp:8000/v1", "http://llama-cpp-small:8000/v1"):
            raise ValueError("Unexpected managed model URL")
        if not key.strip():
            raise ValueError("Managed API keys must not be empty")
        if url not in urls:
            urls.append(url)
            keys.append(key)
        else:
            for i, existing in enumerate(urls):
                if existing == url:
                    keys[i] = key
    updates = {"openai.api_base_urls": urls, "openai.api_keys": keys}
    owner = await users.get_first_user()
    if owner is not None:
        if owner.role != "admin":
            raise RuntimeError("First user must be an administrator")
        from apply_open_terminal import connection_settings
        grants = await terminal_grants(groups, group_form, owner.id)
        updates.update(await connection_settings(config, grants))
        if os.environ.get("CLOSE_SIGNUP_AFTER_BOOTSTRAP", "true").lower() == "true":
            updates["ui.enable_signup"] = False
        if not await config.get(SECURITY_KEY):
            updates["auth.jwt_expiry"] = os.environ.get("JWT_EXPIRES_IN", "4h")
            updates["ui.default_user_role"] = "pending"
    await config.upsert(updates)
    for key, value in updates.items():
        if await config.get(key) != value:
            raise RuntimeError(f"Failed to save security setting {key}; safe to retry")
    if owner is not None:
        await config.upsert({SECURITY_KEY: True})

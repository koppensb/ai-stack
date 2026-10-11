"""Merge the managed terminal into saved Open WebUI settings; restart afterwards."""
import asyncio
import sys
from start import terminal_connection


# Match either stable ID or URL to avoid duplicating a connection created by hand.
# Credentials are refreshed; only the explicitly managed group receives access.
async def connection_settings(config, grants):
    managed = terminal_connection()
    managed["config"]["access_grants"] = grants
    connections = list(await config.get("terminal_server.connections") or [])
    matched = False
    for index, connection in enumerate(connections):
        if connection.get("id") == managed["id"] or connection.get("url", "").rstrip("/") == managed["url"]:
            matched = True
            # Retain other options, but remove old user:* and unrelated grants.
            saved_config = dict(connection.get("config") or {})
            saved_config["access_grants"] = grants
            # Keep identity, name and an intentional disable.
            connections[index] = {**managed, **connection,
                                  "url": managed["url"], "key": managed["key"],
                                  "auth_type": managed["auth_type"],
                                  "config": saved_config}
    if not matched:
        connections.append(managed)
    defaults = dict(await config.get("ui.default_interface_settings") or {})
    defaults["terminalFileDisplay"] = "inline"
    return {"terminal_server.connections": connections,
            "ui.default_interface_settings": defaults}


async def main():
    sys.path.insert(0, "/app/backend")
    from open_webui.models.config import Config
    from open_webui.models.users import Users
    from open_webui.models.groups import Groups, GroupForm
    from apply_security import terminal_grants
    owner = await Users.get_first_user()
    if owner is None:
        print("Terminal group setup deferred until the first administrator signs up.")
        return
    if owner.role != "admin":
        raise RuntimeError("First user must be an administrator")
    grants = await terminal_grants(Groups, GroupForm, owner.id)
    await Config.upsert(await connection_settings(Config, grants))
    print("Open Terminal connection saved. Restart Open WebUI and refresh the browser.")


if __name__ == "__main__":
    asyncio.run(main())

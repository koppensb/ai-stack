"""Merge the managed terminal into saved Open WebUI settings; restart afterwards."""
import asyncio
import sys
from start import terminal_connection


# Match either stable ID or URL to avoid duplicating a connection created by hand.
# Credentials are refreshed from .env and public read access is added.
async def connection_settings(config):
    managed = terminal_connection()
    connections = list(await config.get("terminal_server.connections") or [])
    for index, connection in enumerate(connections):
        if connection.get("id") == managed["id"] or connection.get("url", "").rstrip("/") == managed["url"]:
            # Retain custom config and grants while sharing with all signed-in users.
            saved_config = dict(connection.get("config") or {})
            grants = list(saved_config.get("access_grants") or [])
            for public_grant in managed["config"]["access_grants"]:
                if not any(
                    isinstance(grant, dict)
                    and all(grant.get(key) == value for key, value in public_grant.items())
                    for grant in grants
                ):
                    grants.append(public_grant)
            saved_config["access_grants"] = grants
            # Keep identity, name and an intentional disable.
            connections[index] = {**managed, **connection,
                                  "url": managed["url"], "key": managed["key"],
                                  "auth_type": managed["auth_type"],
                                  "config": saved_config}
            break
    else:
        connections.append(managed)
    return {"terminal_server.connections": connections}


async def main():
    sys.path.insert(0, "/app/backend")
    from open_webui.models.config import Config
    await Config.upsert(await connection_settings(Config))
    print("Open Terminal connection saved. Restart Open WebUI and refresh the browser.")


if __name__ == "__main__":
    asyncio.run(main())

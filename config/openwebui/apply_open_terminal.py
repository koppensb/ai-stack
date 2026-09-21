"""Merge the managed terminal into saved Open WebUI settings; restart afterwards."""
import asyncio
import sys
from start import terminal_connection


# Match either stable ID or URL to avoid duplicating a connection created by hand.
# Credentials are refreshed from .env while existing user access remains intact.
async def connection_settings(config):
    managed = terminal_connection()
    connections = list(await config.get("terminal_server.connections") or [])
    for index, connection in enumerate(connections):
        if connection.get("id") == managed["id"] or connection.get("url", "").rstrip("/") == managed["url"]:
            # Keep identity, access grants, name and an intentional disable.
            connections[index] = {**managed, **connection,
                                  "url": managed["url"], "key": managed["key"],
                                  "auth_type": managed["auth_type"]}
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

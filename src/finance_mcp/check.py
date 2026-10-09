"""Sanity check for the .env file: `python -m finance_mcp.check` (or inside Docker:
`docker compose exec finance-mcp python -m finance_mcp.check`).

Verifies Pluggy credentials, lists each Item's institution and accounts, and checks brapi.
Prints no secrets.
"""

from __future__ import annotations

import asyncio

from .market import MarketData
from .pluggy import PluggyClient, PluggyError
from .settings import load_settings


async def run() -> int:
    s = load_settings()
    ok = True
    pluggy = PluggyClient(s.pluggy_client_id, s.pluggy_client_secret, s.pluggy_base_url)
    try:
        await pluggy._get_api_key()
        print("✔ Pluggy credentials OK")
    except PluggyError as e:
        print(f"✘ {e}")
        return 1

    if not s.pluggy_item_ids:
        print("✘ PLUGGY_ITEM_IDS is empty")
        ok = False
    for item_id in s.pluggy_item_ids:
        try:
            item = await pluggy.get_item(item_id)
            accounts = await pluggy.get_accounts(item_id)
            name = (item.get("connector") or {}).get("name")
            print(f"✔ Item {item_id[:8]}…  {name}  status={item.get('status')}  last sync={item.get('lastUpdatedAt')}")
            for a in accounts:
                print(f"    - {a.get('type'):6} {a.get('subtype') or '':18} {a.get('name')}")
        except PluggyError as e:
            print(f"✘ Item {item_id[:8]}…  {e}")
            ok = False
    await pluggy.aclose()

    market = MarketData(s.brapi_token)
    quote = (await market.quotes(["ITSA4"]))[0]
    if "error" in quote:
        print(f"✘ brapi: {quote['error']}")
        ok = False
    else:
        print(f"✔ brapi OK (ITSA4 = {quote['price']})")
    await market.aclose()

    if s.auth_mode == "github":
        missing = [
            k
            for k, v in {
                "GITHUB_CLIENT_ID": s.github_client_id,
                "GITHUB_CLIENT_SECRET": s.github_client_secret,
                "ALLOWED_GITHUB_USERS": s.allowed_github_users,
                "JWT_SIGNING_KEY": s.jwt_signing_key,
            }.items()
            if not v
        ]
        if missing:
            print(f"✘ Missing for GitHub login: {', '.join(missing)}")
            ok = False
        else:
            print(f"✔ GitHub login configured for: {', '.join(s.allowed_github_users)}")
    print("\nAll good!" if ok else "\nFix the ✘ items above.")
    return 0 if ok else 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

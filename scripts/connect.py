#!/usr/bin/env python3
"""Connect a bank to your Pluggy application and print its Item ID. No dashboard needed.

Run it on YOUR computer (it opens a browser window), from the project folder:

    python3 scripts/connect.py                 # connect a new institution (pick "MeuPluggy")
    python3 scripts/connect.py --update ITEM   # renew/repair an existing connection
    python3 scripts/connect.py --sandbox       # also show Pluggy's test bank

It reads PLUGGY_CLIENT_ID / PLUGGY_CLIENT_SECRET from .env (or the environment).
Only Python's standard library is used, so no install is required.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import urllib.request
import webbrowser
from pathlib import Path

API = "https://api.pluggy.ai"
WIDGET = "https://cdn.pluggy.ai/pluggy-connect/latest/pluggy-connect.js"
PORT = 8899

PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Pluggy Connect</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}
code{background:#eee;padding:4px 8px;border-radius:4px;font-size:1.1em}</style>
<script src="__WIDGET__"></script></head>
<body><h1>Pluggy Connect</h1>
<p id="msg">Opening the Pluggy window… choose <b>MeuPluggy</b> to use the banks you connected at meu.pluggy.ai.</p>
<script>
const show = (html) => document.getElementById('msg').innerHTML = html;
const connect = new PluggyConnect({
  connectToken: __TOKEN__,
  includeSandbox: __SANDBOX__,
  __UPDATE__
  onSuccess: async (data) => {
    const item = data.item || data;
    await fetch('/done', {method: 'POST', body: JSON.stringify({id: item.id, connector: (item.connector || {}).name})});
    show('Connected <b>' + ((item.connector || {}).name || '') + '</b>. Item ID:<br><br><code>' + item.id +
         '</code><br><br>It was also printed in your terminal. You can close this tab.');
  },
  onError: (err) => show('Error: ' + (err && err.message ? err.message : JSON.stringify(err))),
});
connect.init();
</script></body></html>
"""


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def post(path: str, body: dict, api_key: str | None = None) -> dict:
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-KEY", api_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", metavar="ITEM_ID", help="reconnect/renew an existing Item")
    parser.add_argument("--sandbox", action="store_true", help="include Pluggy's sandbox test bank")
    args = parser.parse_args()

    load_env(Path(__file__).resolve().parent.parent / ".env")
    client_id, secret = os.environ.get("PLUGGY_CLIENT_ID"), os.environ.get("PLUGGY_CLIENT_SECRET")
    if not client_id or not secret:
        raise SystemExit("Set PLUGGY_CLIENT_ID and PLUGGY_CLIENT_SECRET in .env first.")

    api_key = post("/auth", {"clientId": client_id, "clientSecret": secret})["apiKey"]
    body: dict = {"options": {"avoidDuplicates": True}}
    if args.update:
        body["itemId"] = args.update
    token = post("/connect_token", body, api_key)["accessToken"]

    page = (
        PAGE.replace("__WIDGET__", WIDGET)
        .replace("__TOKEN__", json.dumps(token))
        .replace("__SANDBOX__", "true" if args.sandbox else "false")
        .replace("__UPDATE__", f"updateItem: {json.dumps(args.update)}," if args.update else "")
    ).encode()

    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            print(f"\n✔ Connected: {data.get('connector')}\n  Item ID: {data.get('id')}\n")
            print("Add it to PLUGGY_ITEM_IDS in your .env (comma-separated if you have several).")
            self.send_response(204)
            self.end_headers()
            done.set()

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)  # local only, never exposed
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Opening {url}  (Ctrl+C to quit)")
    webbrowser.open(url)
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    main()

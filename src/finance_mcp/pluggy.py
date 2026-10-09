"""Minimal async client for the Pluggy API (read-only endpoints only).

Auth: POST /auth with clientId/clientSecret returns an apiKey valid for ~2h,
sent as the X-API-KEY header on every other request.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

API_KEY_TTL_SECONDS = 100 * 60  # Pluggy keys last 2h; renew a bit earlier.
CACHE_TTL_SECONDS = 5 * 60


class PluggyError(RuntimeError):
    pass


class PluggyClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str = "https://api.pluggy.ai"):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30)
        self._api_key: str | None = None
        self._api_key_expires = 0.0
        self._auth_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _get_api_key(self, force: bool = False) -> str:
        async with self._auth_lock:
            if force or not self._api_key or time.monotonic() >= self._api_key_expires:
                resp = await self._http.post(
                    "/auth", json={"clientId": self._client_id, "clientSecret": self._client_secret}
                )
                if resp.status_code >= 400:
                    raise PluggyError(f"Pluggy auth failed ({resp.status_code}). Check PLUGGY_CLIENT_ID/SECRET.")
                self._api_key = resp.json()["apiKey"]
                self._api_key_expires = time.monotonic() + API_KEY_TTL_SECONDS
            return self._api_key

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in (1, 2):
            key = await self._get_api_key(force=attempt == 2)
            resp = await self._http.request(method, path, headers={"X-API-KEY": key}, **kwargs)
            if resp.status_code in (401, 403) and attempt == 1:
                continue  # key expired early: renew once and retry
            if resp.status_code >= 400:
                raise PluggyError(f"Pluggy {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        raise PluggyError("unreachable")

    async def _get_cached(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = f"{path}?{sorted((params or {}).items())}"
        hit = self._cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        data = await self._request("GET", path, params=params)
        self._cache[cache_key] = (time.monotonic(), data)
        return data

    async def _get_all_pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Page-number pagination (accounts, investments, bills)."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get_cached(path, {**params, "page": page, "pageSize": 500})
            if isinstance(data, list):  # some endpoints return a bare list
                return data
            results += data.get("results", [])
            if page >= (data.get("totalPages") or 1):
                return results
            page += 1

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------------------------------------------------------------- endpoints

    async def get_item(self, item_id: str) -> dict[str, Any]:
        return await self._get_cached(f"/items/{item_id}")

    async def refresh_item(self, item_id: str) -> dict[str, Any]:
        """Ask Pluggy to sync this connection with the bank again (uses the existing consent)."""
        self.clear_cache()
        return await self._request("PATCH", f"/items/{item_id}", json={})

    async def get_accounts(self, item_id: str) -> list[dict[str, Any]]:
        return await self._get_all_pages("/accounts", {"itemId": item_id})

    async def get_investments(self, item_id: str) -> list[dict[str, Any]]:
        return await self._get_all_pages("/investments", {"itemId": item_id})

    async def get_bills(self, account_id: str) -> list[dict[str, Any]]:
        return await self._get_all_pages("/bills", {"accountId": account_id})

    async def get_categories(self) -> list[dict[str, Any]]:
        data = await self._get_cached("/categories")
        return data.get("results", data) if isinstance(data, dict) else data

    async def get_transactions(self, account_id: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
        """Cursor pagination via GET /v2/transactions (the page-based /transactions is deprecated)."""
        results: list[dict[str, Any]] = []
        params: dict[str, Any] = {"accountId": account_id, "dateFrom": date_from, "dateTo": date_to}
        while True:
            data = await self._get_cached("/v2/transactions", params)
            results += data.get("results", [])
            nxt = data.get("next")
            if not nxt:
                return results
            after = parse_qs(urlparse(nxt).query).get("after", [None])[0]
            if not after:
                return results
            params = {**params, "after": after}

    async def create_connect_token(self, item_id: str | None = None) -> str:
        body: dict[str, Any] = {"options": {"avoidDuplicates": True}}
        if item_id:
            body["itemId"] = item_id
        data = await self._request("POST", "/connect_token", json=body)
        return data["accessToken"]

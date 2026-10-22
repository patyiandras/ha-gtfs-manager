"""Kliens a GTFS Manager szerver `/api/v1` végpontjaihoz."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

API_VERSION = 1
REQUEST_TIMEOUT = 15


class ApiError(Exception):
    """Általános API-hiba (nem várt HTTP-válasz)."""


class ApiConnectionError(ApiError):
    """A szerver nem érhető el (hálózat, időtúllépés)."""


class ApiNotFound(ApiError):
    """404 — nincs ilyen menetrend / megálló, vagy a szerveren nincs `/api/v1`."""


class ApiInvalid(ApiError):
    """A válasz nem a várt formátumú (nem GTFS Manager, vagy más API-verzió)."""


def normalize_url(raw: str) -> str | None:
    """Kanonikus alap-URL (séma + hoszt [+ útvonal]), záró `/` nélkül.

    Séma nélkül `https://`-t feltételez. Érvénytelen bemenetre `None`.
    """
    url = (raw or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    # Ha valaki a teljes API-útvonalat másolta be, az alapra vágjuk vissza.
    if path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    return f"{parts.scheme}://{parts.netloc}{path}"


def _seg(value: str) -> str:
    return quote(str(value), safe="")


class GtfsManagerApi:
    """Vékony aszinkron kliens; minden hívás JSON-t ad vissza."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(
                    url, params=clean, headers={"Accept": "application/json"}
                ) as resp:
                    if resp.status == 404:
                        raise ApiNotFound(url)
                    if resp.status >= 400:
                        raise ApiError(f"HTTP {resp.status}: {url}")
                    try:
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        raise ApiInvalid(f"Nem JSON válasz: {url}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApiConnectionError(f"{url}: {err}") from err

    async def info(self) -> dict[str, Any]:
        data = await self._get("/info")
        if not isinstance(data, dict) or data.get("api_version") != API_VERSION:
            raise ApiInvalid("Nem támogatott API-verzió")
        return data

    async def search_stops(
        self,
        feed_id: str,
        *,
        query: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: int | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/feeds/{_seg(feed_id)}/stops",
            {"q": query, "lat": lat, "lon": lon, "radius_m": radius_m, "limit": limit},
        )
        return list((data or {}).get("stops") or [])

    async def stop(self, feed_id: str, stop_id: str) -> dict[str, Any]:
        return await self._get(f"/feeds/{_seg(feed_id)}/stops/{_seg(stop_id)}")

    async def departures(
        self,
        feed_id: str,
        stop_id: str,
        *,
        route_ids: list[str] | None = None,
        limit: int | None = None,
        horizon_min: int | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            f"/feeds/{_seg(feed_id)}/stops/{_seg(stop_id)}/departures",
            {
                "route_id": ",".join(route_ids) if route_ids else None,
                "limit": limit,
                "horizon_min": horizon_min,
            },
        )

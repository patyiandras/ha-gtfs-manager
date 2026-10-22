"""Kliens a GTFS Manager szerver `/api/v1` végpontjaihoz.

BIZALMI HATÁR: a szerver válasza KÜLSŐ adat, ami entitás-attribútumba, a
felületre és felolvasott szövegbe kerül. Ezért minden mező itt, a beolvasáskor
megy át típus-, hossz- és karakterszűrésen, a válasz mérete korlátos, és a
kliens nem követ átirányítást (egy feltört vagy rosszindulatú szerver ne tudja
a HA-t a helyi hálózat más címeire küldeni).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

API_VERSION = 1
REQUEST_TIMEOUT = 15
# Egy 50 elemes indulás-lista ~30 kB; ennél jóval nagyobb válasz nem legitim.
MAX_RESPONSE_BYTES = 1_000_000

MAX_TEXT_LEN = 200
MAX_ID_LEN = 128
MAX_FEEDS = 50
MAX_STOPS = 100
MAX_ROUTES = 1000
MAX_DEPARTURES = 50

KNOWN_MODES = frozenset(
    {"tram", "subway", "rail", "suburban", "bus", "coach", "trolleybus", "ferry", "other", "unknown"}
)

# Vezérlőkarakterek, nulla szélességű és irányváltó (bidi) jelek: a felületen
# megtévesztő szöveget lehetne velük előállítani.
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}$")


class ApiError(Exception):
    """Általános API-hiba (nem várt HTTP-válasz).

    A `detail` emberi olvasásra szánt diagnosztika (melyik URL, mi jött vissza,
    ki válaszolt) — a varázsló hibaüzenete mutatja meg, hogy a felhasználó lássa,
    miért nem jó a cím.
    """

    def __init__(self, message: str = "", detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message


class ApiConnectionError(ApiError):
    """A szerver nem érhető el (hálózat, időtúllépés)."""


class ApiNotFound(ApiError):
    """404 — nincs ilyen menetrend / megálló, vagy a szerveren nincs `/api/v1`."""


class ApiHttpError(ApiError):
    """Nem várt HTTP-státusz (pl. 5xx, Cloudflare-blokk). A `status` a varázsló
    hibaüzenetébe kerül, hogy a felhasználó lássa, mi történt."""

    def __init__(self, url: str, status: int, detail: str | None = None) -> None:
        super().__init__(f"HTTP {status}: {url}", detail)
        self.status = status


class ApiMaintenance(ApiHttpError):
    """503 — a szerver karbantartás (pl. frissítés) alatt áll; később újra."""


class ApiRateLimited(ApiHttpError):
    """429 — túl sok kérés erről a címről; később újra."""


class ApiInvalid(ApiError):
    """A válasz nem a várt formátumú (nem GTFS Manager, más API-verzió, túl nagy)."""


class ApiRedirect(ApiError):
    """A szerver átirányít — nem követjük automatikusan (lásd a modul leírását)."""

    def __init__(self, url: str, location: str | None) -> None:
        super().__init__(f"Átirányítás: {url} → {location}")
        self.location = urljoin(url, location) if location else None


# ── Bemenet-tisztítás ──────────────────────────────────────────────────────
def clean_text(value: Any, max_len: int = MAX_TEXT_LEN) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = _UNSAFE_CHARS.sub("", str(value)).strip()
    return text[:max_len] or None


def clean_id(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value)
    if not text or len(text) > MAX_ID_LEN or _UNSAFE_CHARS.search(text) or text in (".", ".."):
        return None
    return text


def clean_int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        num = int(value)
    except (OverflowError, ValueError):
        return None
    return num if lo <= num <= hi else None


def clean_color(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEX_COLOR.match(value) else None


def _dicts(value: Any, cap: int) -> list[dict[str, Any]]:
    return [v for v in value[:cap] if isinstance(v, dict)] if isinstance(value, list) else []


def clean_route(raw: dict[str, Any]) -> dict[str, Any] | None:
    route_id = clean_id(raw.get("route_id"))
    if route_id is None:
        return None
    mode = raw.get("mode")
    return {
        "route_id": route_id,
        "route_label": clean_text(raw.get("route_label"), 64) or route_id,
        "mode": mode if mode in KNOWN_MODES else "unknown",
        "route_color": clean_color(raw.get("route_color")),
        "direction": clean_text(raw.get("direction")),
    }


def clean_line(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Viszonylat (kiírt járatnév + irány), a szerver `lines` tömbjéből."""
    label = clean_text(raw.get("label"), 64)
    if not label:
        return None
    mode = raw.get("mode")
    return {
        "label": label,
        "direction": clean_text(raw.get("direction")),
        "mode": mode if mode in KNOWN_MODES else "unknown",
        "route_color": clean_color(raw.get("route_color")),
        "variants": clean_int(raw.get("variants"), 0, 100_000) or 1,
    }


def lines_from_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tartalék régebbi szerverhez, amely még nem ad `lines`-t: helyben vonjuk össze."""
    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for r in routes:
        key = (r["route_label"], r.get("direction"))
        if key in out:
            out[key]["variants"] += 1
        else:
            out[key] = {
                "label": r["route_label"],
                "direction": r.get("direction"),
                "mode": r.get("mode", "unknown"),
                "route_color": r.get("route_color"),
                "variants": 1,
            }
    return list(out.values())


def clean_stop(raw: dict[str, Any]) -> dict[str, Any] | None:
    stop_id = clean_id(raw.get("stop_id"))
    if stop_id is None:
        return None
    routes = [r for r in (clean_route(x) for x in _dicts(raw.get("routes"), MAX_ROUTES)) if r]
    return {
        "stop_id": stop_id,
        "stop_name": clean_text(raw.get("stop_name")) or stop_id,
        "stop_desc": clean_text(raw.get("stop_desc")),
        "distance_m": clean_int(raw.get("distance_m"), 0, 100_000),
        "is_station": raw.get("is_station") is True,
        "routes": routes,
        "lines": [l for l in (clean_line(x) for x in _dicts(raw.get("lines"), MAX_ROUTES)) if l]
        or lines_from_routes(routes),
    }


def clean_departure(raw: dict[str, Any]) -> dict[str, Any] | None:
    route = clean_route(raw)
    trip_id = clean_id(raw.get("trip_id"))
    scheduled = clean_text(raw.get("scheduled_at"), 40)
    expected = clean_text(raw.get("expected_at"), 40)
    if route is None or trip_id is None or not scheduled or not expected:
        return None
    return {
        **route,
        "trip_id": trip_id,
        "stop_id": clean_id(raw.get("stop_id")) or "",
        "headsign": clean_text(raw.get("headsign")),
        "scheduled_at": scheduled,
        "expected_at": expected,
        # A szerver késés-sávja -10 … +60 perc; ami ezen kívül esik, az nem késés.
        "delay_sec": clean_int(raw.get("delay_sec"), -6 * 3600, 6 * 3600),
        "realtime": raw.get("realtime") is True,
        "tracked": raw.get("tracked") is True,
        "canceled": raw.get("canceled") is True,
    }


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
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        parts.port  # noqa: B018 — érvénytelen port (pl. „x:alert(1)") itt dob
    except ValueError:
        return None
    # Felhasználónév/jelszó az URL-ben: a naplóba és a bejegyzés adataiba kerülne.
    if parts.username or parts.password:
        return None
    path = parts.path.rstrip("/")
    # Ha valaki a teljes API-útvonalat másolta be, az alapra vágjuk vissza.
    for suffix in ("/api/v1/info", "/api/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parts.scheme}://{parts.netloc}{path}"


def _seg(value: str) -> str:
    return quote(str(value), safe="")


class GtfsManagerApi:
    """Vékony aszinkron kliens; a válaszokat tisztított alakban adja vissza."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(
                    url,
                    params=clean,
                    headers={"Accept": "application/json"},
                    allow_redirects=False,
                ) as resp:
                    if 300 <= resp.status < 400:
                        raise ApiRedirect(url, resp.headers.get("Location"))
                    # Ki válaszolt? A Cloudflare-en át érkező válaszban van CF-RAY;
                    # ha nincs, a kérés helyben (pl. belső DNS-sel) máshová ment.
                    via = "Cloudflare" if "CF-RAY" in resp.headers else (
                        "nem Cloudflare: " + (clean_text(resp.headers.get("Server"), 40) or "?")
                    )
                    ctype = clean_text(resp.headers.get("Content-Type", "?").split(";")[0], 40)
                    detail = f"{url} → HTTP {resp.status}, {ctype}, {via}"
                    if resp.status == 404:
                        raise ApiNotFound(url, detail)
                    if resp.status == 503:
                        raise ApiMaintenance(url, resp.status, detail)
                    if resp.status == 429:
                        raise ApiRateLimited(url, resp.status, detail)
                    if resp.status >= 400:
                        raise ApiHttpError(url, resp.status, detail)
                    body = await resp.content.read(MAX_RESPONSE_BYTES + 1)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApiConnectionError(f"{url}: {err}") from err
        if len(body) > MAX_RESPONSE_BYTES:
            raise ApiInvalid(f"Túl nagy válasz: {url}", detail)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as err:
            raise ApiInvalid(f"Nem JSON válasz: {url}", f"{detail} (nem JSON)") from err

    async def info(self) -> dict[str, Any]:
        data = await self._get("/info")
        if not isinstance(data, dict) or data.get("api_version") != API_VERSION:
            version = data.get("api_version") if isinstance(data, dict) else None
            raise ApiInvalid(
                "Nem támogatott API-verzió",
                f"{self.base_url}/api/v1/info → JSON, api_version={version!r}",
            )
        feeds = []
        for raw in _dicts(data.get("feeds"), MAX_FEEDS):
            feed_id = clean_id(raw.get("id"))
            if feed_id:
                feeds.append({"id": feed_id, "name": clean_text(raw.get("name")) or feed_id})
        return {"api_version": API_VERSION, "feeds": feeds}

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
        stops = _dicts(data.get("stops") if isinstance(data, dict) else None, MAX_STOPS)
        return [s for s in (clean_stop(x) for x in stops) if s]

    async def stop(self, feed_id: str, stop_id: str) -> dict[str, Any]:
        data = await self._get(f"/feeds/{_seg(feed_id)}/stops/{_seg(stop_id)}")
        if not isinstance(data, dict):
            raise ApiInvalid("Hibás megálló-válasz")
        routes = [r for r in (clean_route(x) for x in _dicts(data.get("routes"), MAX_ROUTES)) if r]
        lines = [l for l in (clean_line(x) for x in _dicts(data.get("lines"), MAX_ROUTES)) if l]
        return {"routes": routes, "lines": lines or lines_from_routes(routes)}

    async def departures(
        self,
        feed_id: str,
        stop_id: str,
        *,
        route_ids: list[str] | None = None,
        lines: list[list[str | None]] | None = None,
        limit: int | None = None,
        horizon_min: int | None = None,
    ) -> dict[str, Any]:
        data = await self._get(
            f"/feeds/{_seg(feed_id)}/stops/{_seg(stop_id)}/departures",
            {
                "route_id": ",".join(route_ids) if route_ids else None,
                "lines": json.dumps(lines, ensure_ascii=False) if lines else None,
                "limit": limit,
                "horizon_min": horizon_min,
            },
        )
        if not isinstance(data, dict):
            raise ApiInvalid("Hibás indulás-válasz")
        stop = data.get("stop") if isinstance(data.get("stop"), dict) else {}
        deps = _dicts(data.get("departures"), MAX_DEPARTURES)
        return {
            "stop": {"stop_name": clean_text(stop.get("stop_name"))},
            "departures": [d for d in (clean_departure(x) for x in deps) if d],
        }

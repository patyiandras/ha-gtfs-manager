"""Beállító varázsló: szerver → menetrend → megálló → járat(ok)."""

from __future__ import annotations

from collections import Counter
import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    ApiConnectionError,
    ApiError,
    ApiRedirect,
    GtfsManagerApi,
    normalize_url,
)
from .const import (
    CONF_DEPARTURES,
    CONF_FEED_ID,
    CONF_FEED_NAME,
    CONF_HORIZON,
    CONF_ROUTE_IDS,
    CONF_ROUTE_LABELS,
    CONF_SCAN_INTERVAL,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_DEPARTURES,
    DEFAULT_HORIZON,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_DEPARTURES,
    MAX_HORIZON,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    NEARBY_RADIUS_M,
)

_LOGGER = logging.getLogger(__name__)

CONF_QUERY = "query"


def _route_option(r: dict[str, Any]) -> SelectOptionDict:
    label = r.get("route_label") or r.get("route_id")
    if r.get("direction"):
        label = f"{label} → {r['direction']}"
    return SelectOptionDict(value=str(r["route_id"]), label=str(label))


def _stop_label(s: dict[str, Any]) -> str:
    """„Határ út M · 50 · → Pestszentlőrinc, Béke tér · 7 m"."""
    parts = [s.get("stop_name") or s.get("stop_id")]
    if s.get("stop_desc"):
        parts[0] = f"{parts[0]} ({s['stop_desc']})"
    routes = s.get("routes") or []
    if routes:
        names = [r.get("route_label") or r.get("route_id") for r in routes]
        shown = ", ".join(names[:10]) + (" …" if len(names) > 10 else "")
        parts.append(shown)
        # Az azonos nevű peronokat az IRÁNY különbözteti meg.
        dirs = Counter(r["direction"] for r in routes if r.get("direction"))
        if dirs:
            parts.append("→ " + " / ".join(d for d, _ in dirs.most_common(2)))
    if s.get("distance_m") is not None:
        parts.append(f"{s['distance_m']} m")
    if s.get("is_station"):
        parts.append("állomás")
    return " · ".join(str(p) for p in parts)


class GtfsManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Egy bejegyzés = egy megálló (opcionálisan kiválasztott járatokra szűrve)."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: GtfsManagerApi | None = None
        self._url: str | None = None
        self._feeds: list[dict[str, Any]] = []
        self._feed: dict[str, Any] | None = None
        self._stops: list[dict[str, Any]] = []
        self._stop: dict[str, Any] | None = None
        self._routes: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GtfsManagerOptionsFlow()

    def _known_url(self) -> str | None:
        """A már felvett megállók szerver-címe — kényelmi kitöltés, nem beégetett alapérték."""
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_URL):
                return entry.data[CONF_URL]
        return None

    # ── 1. Szerver ──────────────────────────────────────────────────────────
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                try:
                    api, info = await self._probe(url)
                except ApiRedirect:
                    errors["base"] = "redirect_other_host"
                except ApiConnectionError:
                    errors["base"] = "cannot_connect"
                except ApiError:
                    errors["base"] = "invalid_api"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Váratlan hiba a szerver ellenőrzésekor")
                    errors["base"] = "unknown"
                else:
                    feeds = info.get("feeds") or []
                    if not feeds:
                        errors["base"] = "no_feeds"
                    else:
                        self._api, self._url, self._feeds = api, api.base_url, feeds
                        if len(feeds) == 1:
                            self._feed = feeds[0]
                            return await self.async_step_stop_search()
                        return await self.async_step_feed()

        suggested = (user_input or {}).get(CONF_URL) or self._known_url()
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL))}
            ),
            {CONF_URL: suggested} if suggested else {},
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            # Csak minta a súgószövegben (a HA a fordításban nem enged URL-t).
            description_placeholders={"example": "https://transit.example.org"},
        )

    async def _probe(self, url: str) -> tuple[GtfsManagerApi, dict[str, Any]]:
        """A szerver ellenőrzése. Átirányítást CSAK ugyanazon a hoszton követünk
        (pl. http → https, vagy alútvonal), egyszer — idegen hosztra nem: a
        bejegyzésbe az kerüljön, amit a felhasználó ténylegesen megadott."""
        session = async_get_clientsession(self.hass)
        api = GtfsManagerApi(session, url)
        try:
            return api, await api.info()
        except ApiRedirect as redirect:
            target = normalize_url(redirect.location or "")
            if not target or urlsplit(target).hostname != urlsplit(url).hostname:
                raise
            api = GtfsManagerApi(session, target)
            return api, await api.info()

    # ── 2. Menetrend ────────────────────────────────────────────────────────
    async def async_step_feed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._feed = next(
                (f for f in self._feeds if f["id"] == user_input.get(CONF_FEED_ID)), None
            )
            if self._feed is None:
                return self.async_abort(reason="invalid_selection")
            return await self.async_step_stop_search()
        options = [SelectOptionDict(value=f["id"], label=f.get("name") or f["id"]) for f in self._feeds]
        return self.async_show_form(
            step_id="feed",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FEED_ID): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    # ── 3. Megálló keresése ─────────────────────────────────────────────────
    async def async_step_stop_search(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._api and self._feed
        errors: dict[str, str] = {}
        if user_input is not None:
            # A szerver 64 karakternél hosszabb keresést elutasít.
            query = (user_input.get(CONF_QUERY) or "").strip()[:64]
            try:
                if len(query) >= 2:
                    stops = await self._api.search_stops(self._feed["id"], query=query, limit=40)
                elif query:
                    stops = None
                    errors[CONF_QUERY] = "query_too_short"
                else:
                    # Üres keresés: az otthon (HA-beállítás) közelében lévő megállók.
                    stops = await self._api.search_stops(
                        self._feed["id"],
                        lat=self.hass.config.latitude,
                        lon=self.hass.config.longitude,
                        radius_m=NEARBY_RADIUS_M,
                        limit=40,
                    )
            except ApiConnectionError:
                errors["base"] = "cannot_connect"
            except ApiError:
                errors["base"] = "invalid_api"
            else:
                if stops is not None and not stops:
                    errors["base"] = "no_stops"
                elif stops:
                    self._stops = stops
                    return await self.async_step_stop()

        return self.async_show_form(
            step_id="stop_search",
            data_schema=vol.Schema({vol.Optional(CONF_QUERY): TextSelector()}),
            description_placeholders={
                "feed": self._feed.get("name") or self._feed["id"],
                "radius": str(NEARBY_RADIUS_M),
            },
            errors=errors,
        )

    # ── 4. Megálló kiválasztása ─────────────────────────────────────────────
    async def async_step_stop(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._api and self._feed
        errors: dict[str, str] = {}
        if user_input is not None:
            self._stop = next(
                (s for s in self._stops if s["stop_id"] == user_input.get(CONF_STOP_ID)), None
            )
            if self._stop is None:
                return self.async_abort(reason="invalid_selection")
            try:
                detail = await self._api.stop(self._feed["id"], self._stop["stop_id"])
            except ApiConnectionError:
                errors["base"] = "cannot_connect"
            except ApiError:
                errors["base"] = "invalid_api"
            else:
                self._routes = detail.get("routes") or []
                return await self.async_step_routes()

        options = [SelectOptionDict(value=s["stop_id"], label=_stop_label(s)) for s in self._stops]
        return self.async_show_form(
            step_id="stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP_ID): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            errors=errors,
        )

    # ── 5. Járatok + név ────────────────────────────────────────────────────
    async def async_step_routes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._url and self._feed and self._stop
        if user_input is not None:
            known = {r["route_id"] for r in self._routes}
            route_ids = sorted(r for r in (user_input.get(CONF_ROUTE_IDS) or []) if r in known)
            labels = [
                r.get("route_label") or r["route_id"]
                for r in self._routes
                if r["route_id"] in route_ids
            ]
            await self.async_set_unique_id(
                f"{self._url}|{self._feed['id']}|{self._stop['stop_id']}|{','.join(route_ids)}"
            )
            self._abort_if_unique_id_configured()
            title = (user_input.get(CONF_NAME) or "").strip()[:100] or self._default_title(labels)
            return self.async_create_entry(
                title=title,
                data={
                    CONF_URL: self._url,
                    CONF_FEED_ID: self._feed["id"],
                    CONF_FEED_NAME: self._feed.get("name") or self._feed["id"],
                    CONF_STOP_ID: self._stop["stop_id"],
                    CONF_STOP_NAME: self._stop.get("stop_name") or self._stop["stop_id"],
                    CONF_ROUTE_IDS: route_ids,
                    CONF_ROUTE_LABELS: labels,
                },
                options={
                    CONF_DEPARTURES: DEFAULT_DEPARTURES,
                    CONF_HORIZON: DEFAULT_HORIZON,
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                },
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_ROUTE_IDS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=[_route_option(r) for r in self._routes],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_NAME): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="routes",
            data_schema=schema,
            description_placeholders={"stop": _stop_label(self._stop)},
        )

    def _default_title(self, labels: list[str]) -> str:
        name = self._stop.get("stop_name") or self._stop["stop_id"]
        return f"{name} – {', '.join(labels)}" if labels else name


class GtfsManagerOptionsFlow(OptionsFlow):
    """Járat-szűrés, elemszám, időablak, frissítési gyakoriság."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        coordinator = getattr(entry, "runtime_data", None)
        routes: list[dict[str, Any]] = []
        if coordinator is not None:
            try:
                detail = await coordinator.api.stop(entry.data[CONF_FEED_ID], entry.data[CONF_STOP_ID])
                routes = detail.get("routes") or []
            except ApiError:
                routes = []

        current_routes = entry.options.get(CONF_ROUTE_IDS, entry.data.get(CONF_ROUTE_IDS)) or []
        if user_input is not None:
            if routes and CONF_ROUTE_IDS in user_input:
                known = {r["route_id"] for r in routes}
                route_ids = sorted(r for r in user_input[CONF_ROUTE_IDS] if r in known)
            else:
                # A járatlista most nem volt lekérhető, a mező nem is látszott:
                # a meglévő szűrő marad (különben némán „minden járat" lenne).
                route_ids = sorted(current_routes)
            return self.async_create_entry(
                data={
                    CONF_ROUTE_IDS: route_ids,
                    CONF_DEPARTURES: int(user_input[CONF_DEPARTURES]),
                    CONF_HORIZON: int(user_input[CONF_HORIZON]),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                }
            )

        fields: dict[Any, Any] = {}
        if routes:
            fields[vol.Optional(CONF_ROUTE_IDS, default=list(current_routes))] = SelectSelector(
                SelectSelectorConfig(
                    options=[_route_option(r) for r in routes],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        fields.update(
            {
                vol.Required(
                    CONF_DEPARTURES,
                    default=entry.options.get(CONF_DEPARTURES, DEFAULT_DEPARTURES),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=MAX_DEPARTURES, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_HORIZON, default=entry.options.get(CONF_HORIZON, DEFAULT_HORIZON)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=MAX_HORIZON,
                        step=10,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="perc",
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))

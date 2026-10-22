"""Adatfrissítés: egy config entry = egy megálló (opcionálisan járatokra szűrve)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiConnectionError, ApiError, ApiNotFound, GtfsManagerApi
from .const import (
    CONF_DEPARTURES,
    CONF_FEED_ID,
    CONF_HORIZON,
    CONF_LINES,
    CONF_ROUTE_IDS,
    CONF_SCAN_INTERVAL,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_DEPARTURES,
    DEFAULT_HORIZON,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .speech import build_speech

_LOGGER = logging.getLogger(__name__)


@dataclass
class Departure:
    """Egy indulás a szerver válaszából, HA-barát alakban."""

    route_id: str
    route_label: str
    mode: str
    color: str | None
    headsign: str | None
    scheduled: datetime
    expected: datetime
    delay_sec: int | None
    realtime: bool
    tracked: bool
    canceled: bool
    trip_id: str
    stop_id: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Departure | None:
        try:
            scheduled = dt_util.parse_datetime(raw.get("scheduled_at") or "")
            expected = dt_util.parse_datetime(raw.get("expected_at") or "")
        except ValueError:
            return None
        # Időzóna nélküli időpont nem hasonlítható a mostanihoz.
        if scheduled is None or expected is None or None in (scheduled.tzinfo, expected.tzinfo):
            return None
        color = raw.get("route_color")
        return cls(
            route_id=str(raw.get("route_id", "")),
            route_label=str(raw.get("route_label") or ""),
            mode=str(raw.get("mode") or "unknown"),
            color=f"#{color}" if color else None,
            headsign=raw.get("headsign"),
            scheduled=scheduled,
            expected=expected,
            delay_sec=raw.get("delay_sec"),
            realtime=bool(raw.get("realtime")),
            tracked=bool(raw.get("tracked")),
            canceled=bool(raw.get("canceled")),
            trip_id=str(raw.get("trip_id", "")),
            stop_id=str(raw.get("stop_id", "")),
        )

    def minutes(self, now: datetime) -> int:
        return max(0, int((self.expected - now).total_seconds() // 60))

    def as_dict(self, now: datetime) -> dict[str, Any]:
        """Attribútum / szolgáltatás-válasz alak (JSON-szerializálható)."""
        delay_min = None if self.delay_sec is None else round(self.delay_sec / 60)
        return {
            "route": self.route_label,
            "route_id": self.route_id,
            "mode": self.mode,
            "headsign": self.headsign,
            "scheduled": dt_util.as_local(self.scheduled).strftime("%H:%M"),
            "expected": dt_util.as_local(self.expected).strftime("%H:%M"),
            "expected_at": self.expected.isoformat(),
            "minutes": self.minutes(now),
            "delay_min": delay_min,
            "realtime": self.realtime,
            "canceled": self.canceled,
            "color": self.color,
        }

    def speech_input(self, now: datetime) -> dict[str, Any]:
        return {
            "route_label": self.route_label,
            "mode": self.mode,
            "headsign": self.headsign,
            "minutes": self.minutes(now),
            "clock": dt_util.as_local(self.expected).strftime("%H:%M"),
            "delay_sec": self.delay_sec,
            "realtime": self.realtime,
            "canceled": self.canceled,
        }


@dataclass
class StopData:
    """Egy frissítés eredménye."""

    stop_name: str
    departures: list[Departure] = field(default_factory=list)

    @property
    def next(self) -> Departure | None:
        return next((d for d in self.departures if not d.canceled), None)

    def speech(self, horizon_min: int | None = None) -> str:
        now = dt_util.utcnow()
        return build_speech(
            [d.speech_input(now) for d in self.departures],
            stop_name=self.stop_name,
            horizon_min=horizon_min,
        )


def parse_departures(payload: dict[str, Any], fallback_stop_name: str) -> StopData:
    stop = payload.get("stop") or {}
    deps = [Departure.from_api(d) for d in payload.get("departures") or []]
    return StopData(
        stop_name=stop.get("stop_name") or fallback_stop_name,
        departures=[d for d in deps if d is not None],
    )


class GtfsManagerCoordinator(DataUpdateCoordinator[StopData]):
    """Egy megálló indulásainak időszakos lekérése."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: GtfsManagerApi) -> None:
        self.entry = entry
        self.api = api
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
        )

    @property
    def url(self) -> str:
        return self.entry.data[CONF_URL]

    @property
    def limit(self) -> int:
        return int(self.entry.options.get(CONF_DEPARTURES, DEFAULT_DEPARTURES))

    @property
    def horizon(self) -> int:
        return int(self.entry.options.get(CONF_HORIZON, DEFAULT_HORIZON))

    @property
    def route_ids(self) -> list[str]:
        return list(
            self.entry.options.get(CONF_ROUTE_IDS, self.entry.data.get(CONF_ROUTE_IDS)) or []
        )

    @property
    def lines(self) -> list[list[str | None]]:
        return list(self.entry.options.get(CONF_LINES, self.entry.data.get(CONF_LINES)) or [])

    async def fetch(self, limit: int | None = None) -> StopData:
        lines = self.lines
        payload = await self.api.departures(
            self.entry.data[CONF_FEED_ID],
            self.entry.data[CONF_STOP_ID],
            # Viszonylat-szűrő esetén a régi (azonosító-alapú) szűrő nem kell.
            route_ids=None if lines else self.route_ids,
            lines=lines or None,
            limit=limit or self.limit,
            horizon_min=self.horizon,
        )
        return parse_departures(payload, self.entry.data.get(CONF_STOP_NAME, ""))

    async def _async_update_data(self) -> StopData:
        try:
            return await self.fetch()
        except ApiNotFound as err:
            raise UpdateFailed(
                f"A megálló vagy a menetrend nem található a szerveren: {err}"
            ) from err
        except ApiConnectionError as err:
            raise UpdateFailed(f"A szerver nem érhető el: {err}") from err
        except ApiError as err:
            raise UpdateFailed(f"Hibás válasz a szervertől: {err}") from err

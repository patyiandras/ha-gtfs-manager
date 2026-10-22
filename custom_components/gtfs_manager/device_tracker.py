"""Device tracker: a megálló felé tartó következő jármű élő helye (térkép, zónák)."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import GtfsManagerConfigEntry
from .const import DEFAULT_ICON, MODE_ICONS
from .coordinator import Departure, GtfsManagerCoordinator
from .sensor import GtfsManagerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GtfsManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([NextVehicleTracker(entry.runtime_data)])


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Légvonalbeli távolság méterben (haversine)."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return round(6_371_000 * 2 * asin(sqrt(a)))


class NextVehicleTracker(GtfsManagerEntity, TrackerEntity):
    """A legkorábbi olyan indulás járműve, amelynek élő pozíciója van.

    Ha egyik figyelt menetnek sincs pozíciója (pl. a MÁV-vonatok egy része, vagy
    menetrend szerinti kivetítés), az állapot ismeretlen — rossz helyet nem
    rajzolunk. A `distance_m` attribútum a megállótól mért légvonalbeli távolság;
    `numeric_state` triggerrel automatizálható (pl. 1500 m alatt: indulj).
    """

    _attr_source_type = SourceType.GPS
    _attr_location_accuracy = 0

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "next_vehicle")

    @property
    def _vehicle_dep(self) -> Departure | None:
        data = self.coordinator.data
        return data.next_vehicle if data else None

    @property
    def latitude(self) -> float | None:
        dep = self._vehicle_dep
        return dep.vehicle["lat"] if dep and dep.vehicle else None

    @property
    def longitude(self) -> float | None:
        dep = self._vehicle_dep
        return dep.vehicle["lon"] if dep and dep.vehicle else None

    @property
    def icon(self) -> str:
        dep = self._vehicle_dep
        return MODE_ICONS.get(dep.mode, DEFAULT_ICON) if dep else DEFAULT_ICON

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dep = self._vehicle_dep
        data = self.coordinator.data
        if not dep or not dep.vehicle or not data:
            return {}
        d = dep.as_dict(dt_util.utcnow())
        v = dep.vehicle
        distance = (
            _distance_m(v["lat"], v["lon"], data.stop_lat, data.stop_lon)
            if data.stop_lat is not None and data.stop_lon is not None
            else None
        )
        return {
            "route": d["route"],
            "headsign": d["headsign"],
            "scheduled": d["scheduled"],
            "expected": d["expected"],
            "minutes": d["minutes"],
            "delay_min": d["delay_min"],
            "distance_m": distance,
            "bearing": v.get("bearing"),
            "speed_kmh": v.get("speed_kmh"),
            "position_age_sec": v.get("age_sec"),
        }

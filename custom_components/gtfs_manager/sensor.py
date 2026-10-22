"""Szenzorok: következő indulás (időpont, perc, késés) + a közeli menetrend."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import CONF_URL, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import GtfsManagerConfigEntry
from .const import CONF_FEED_NAME, DEFAULT_ICON, DOMAIN, MODE_ICONS
from .coordinator import Departure, GtfsManagerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GtfsManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NextDepartureSensor(coordinator),
            NextDepartureMinutesSensor(coordinator),
            DelaySensor(coordinator),
            TimetableSensor(coordinator),
        ]
    )


class GtfsManagerEntity(CoordinatorEntity[GtfsManagerCoordinator]):
    """Közös alap: eszköz (megálló), egyedi azonosító, fordítható név."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GtfsManagerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="GTFS Manager",
            model=entry.data.get(CONF_FEED_NAME) or "Megálló",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data[CONF_URL],
        )

    @property
    def suggested_object_id(self) -> str | None:
        """Az entitás-azonosító MINDIG angol (`…_delay_alert`), a felület nyelvétől
        függetlenül; a megjelenített név marad a fordított (pl. „Késés van")."""
        return self.translation_key.replace("_", " ") if self.translation_key else None

    @property
    def _next(self) -> Departure | None:
        return self.coordinator.data.next if self.coordinator.data else None


class _Base(GtfsManagerEntity, SensorEntity):
    @property
    def icon(self) -> str:
        nxt = self._next
        return MODE_ICONS.get(nxt.mode, DEFAULT_ICON) if nxt else DEFAULT_ICON


class NextDepartureSensor(_Base):
    """A következő (nem törölt) indulás VÁRHATÓ időpontja."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # A felolvasható mondat percenként változik — nem kell a recorderbe.
    _unrecorded_attributes = frozenset({"speech", "minutes"})

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "next_departure")

    @property
    def native_value(self) -> datetime | None:
        nxt = self._next
        return nxt.expected if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        nxt = self._next
        attrs: dict[str, Any] = {
            "stop": data.stop_name if data else None,
            "speech": data.speech(self.coordinator.horizon) if data else None,
        }
        if nxt:
            now = dt_util.utcnow()
            d = nxt.as_dict(now)
            attrs.update(
                {
                    "route": d["route"],
                    "headsign": d["headsign"],
                    "mode": d["mode"],
                    "scheduled": d["scheduled"],
                    "expected": d["expected"],
                    "minutes": d["minutes"],
                    "delay_min": d["delay_min"],
                    "realtime": d["realtime"],
                    "color": d["color"],
                }
            )
        return attrs


class NextDepartureMinutesSensor(_Base):
    """Hány perc múlva indul a következő — kártyákhoz, automatizmusokhoz."""

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "next_departure_minutes")

    @property
    def native_value(self) -> int | None:
        nxt = self._next
        return nxt.minutes(dt_util.utcnow()) if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self._next
        if not nxt:
            return {}
        return {
            "route": nxt.route_label,
            "headsign": nxt.headsign,
            "realtime": nxt.realtime,
        }


class DelaySensor(_Base):
    """A következő indulás késése percben (negatív = korábban jön).

    Ha a menetről nincs élő adat, az állapot ismeretlen — a „0 perc" azt
    állítaná, hogy pontos, holott nem tudjuk.
    """

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "delay")

    @property
    def icon(self) -> str:
        return "mdi:clock-alert-outline"

    @property
    def native_value(self) -> int | None:
        nxt = self._next
        if not nxt or not nxt.realtime or nxt.delay_sec is None:
            return None
        return round(nxt.delay_sec / 60)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self._next
        if not nxt:
            return {}
        d = nxt.as_dict(dt_util.utcnow())
        return {
            "route": d["route"],
            "headsign": d["headsign"],
            "scheduled": d["scheduled"],
            "expected": d["expected"],
            "realtime": d["realtime"],
        }


class TimetableSensor(_Base):
    """A következő indulások listája (attribútumban); az állapot a darabszám."""

    _attr_native_unit_of_measurement = "indulás"
    _unrecorded_attributes = frozenset({"departures", "speech"})

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "timetable")

    @property
    def icon(self) -> str:
        return "mdi:timetable"

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return len(data.departures) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        now = dt_util.utcnow()
        return {
            "stop": data.stop_name,
            "horizon_min": self.coordinator.horizon,
            "departures": [d.as_dict(now) for d in data.departures],
            "speech": data.speech(self.coordinator.horizon),
        }

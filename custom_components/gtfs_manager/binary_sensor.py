"""Bináris szenzor: van-e jelentős késés vagy elmaradó menet a figyelt indulások közt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import GtfsManagerConfigEntry
from .coordinator import GtfsManagerCoordinator
from .sensor import GtfsManagerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GtfsManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([DelayAlertBinarySensor(entry.runtime_data)])


class DelayAlertBinarySensor(GtfsManagerEntity, BinarySensorEntity):
    """BE, ha a lista valamely indulása legalább a küszöbnyit késik, vagy elmarad.

    A lista ugyanaz, mint a Menetrend szenzoré (a beállított darabszám és
    időablak). Az élő adat nélküli indulás nem számít későnek — arról nem tudjuk.
    Automatizmushoz: kapcsoláskor a `speech` attribútumot érdemes felolvastatni.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _unrecorded_attributes = frozenset({"speech", "items"})

    def __init__(self, coordinator: GtfsManagerCoordinator) -> None:
        super().__init__(coordinator, "delay_alert")

    @property
    def icon(self) -> str:
        return "mdi:bus-alert" if self.is_on else "mdi:bus-clock"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return bool(data.delayed(self.coordinator.delay_threshold))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        threshold = self.coordinator.delay_threshold
        if data is None:
            return {"threshold_min": threshold}
        now = dt_util.utcnow()
        hits = data.delayed(threshold)
        delays = [d.delay_sec for d in hits if not d.canceled and d.delay_sec is not None]
        return {
            "threshold_min": threshold,
            "delayed_count": sum(1 for d in hits if not d.canceled),
            "canceled_count": sum(1 for d in hits if d.canceled),
            "max_delay_min": round(max(delays) / 60) if delays else None,
            "items": [d.as_dict(now) for d in hits],
            "speech": data.delay_speech(threshold),
        }

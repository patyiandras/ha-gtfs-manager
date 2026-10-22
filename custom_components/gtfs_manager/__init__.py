"""GTFS Manager — élő indulási idők megállónként, egy GTFS Manager szerverről."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util, slugify

from .api import ApiError, GtfsManagerApi
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_LIMIT,
    DOMAIN,
    LEGACY_HU_NAMES,
    MAX_DEPARTURES,
    SERVICE_GET_DEPARTURES,
)
from .coordinator import GtfsManagerCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type GtfsManagerConfigEntry = ConfigEntry[GtfsManagerCoordinator]

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_LIMIT): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DEPARTURES)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """A `get_departures` szolgáltatás: friss lekérés + felolvasható mondat.

    Szkriptekből, automatizmusokból és LLM-asszisztensből (pl. Google Gemini
    beszélgető-ügynök) hívható; a válasz az indulások listája és egy kész
    magyar mondat (`speech`).
    """

    async def _get_departures(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry: GtfsManagerConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"Ismeretlen GTFS Manager bejegyzés: {entry_id}")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"A bejegyzés nincs betöltve: {entry.title}")
        coordinator = entry.runtime_data
        try:
            data = await coordinator.fetch(call.data.get(ATTR_LIMIT))
        except ApiError as err:
            raise HomeAssistantError(f"A GTFS Manager szerver nem válaszolt: {err}") from err
        now = dt_util.utcnow()
        result: dict[str, Any] = {
            "stop": data.stop_name,
            "speech": data.speech(coordinator.horizon),
            # Csak a késések / elmaradó menetek (a beállított küszöb szerint).
            "delay_speech": data.delay_speech(coordinator.delay_threshold),
            "delayed": [d.as_dict(now) for d in data.delayed(coordinator.delay_threshold)],
            "departures": [d.as_dict(now) for d in data.departures],
        }
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DEPARTURES,
        _get_departures,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GtfsManagerConfigEntry) -> bool:
    api = GtfsManagerApi(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = GtfsManagerCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _async_migrate_entity_ids(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


def _async_migrate_entity_ids(hass: HomeAssistant, entry: GtfsManagerConfigEntry) -> None:
    """Magyar (automatikusan képzett) entitás-azonosítók → angol.

    Csak azt nevezi át, ami pontosan a régi automatikus alak volt
    (`<eszköznév>_<magyar név>`): a felhasználó által átnevezett azonosítóhoz
    nem nyúl, és ütközés esetén (a cél már foglalt) sem.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for reg in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        key = reg.unique_id.removeprefix(prefix)
        hu_name = LEGACY_HU_NAMES.get(key)
        if not hu_name or reg.unique_id == key:
            continue
        device = dev_reg.async_get(reg.device_id) if reg.device_id else None
        device_name = (device.name_by_user or device.name) if device else entry.title
        domain = reg.domain
        legacy = f"{domain}.{slugify(f'{device_name} {hu_name}')}"
        target = f"{domain}.{slugify(f'{device_name} {key}')}"
        if reg.entity_id != legacy or legacy == target or ent_reg.async_get(target):
            continue
        ent_reg.async_update_entity(reg.entity_id, new_entity_id=target)


async def _async_reload_on_options(hass: HomeAssistant, entry: GtfsManagerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GtfsManagerConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


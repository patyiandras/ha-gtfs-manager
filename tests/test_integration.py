"""Végig a varázslón, majd szenzorok és a `get_departures` szolgáltatás — mockolt szerverrel.

Futtatás: `pip install pytest-homeassistant-custom-component`, majd `pytest`.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.gtfs_manager.const import DOMAIN

BASE = "https://transit.example.org"


def _iso(d):
    return d.isoformat().replace("+00:00", "Z")


def _dep(trip, sched, exp, *, realtime, delay):
    return {
        "route_id": "R50",
        "route_short_name": "50",
        "route_label": "50",
        "mode": "tram",
        "route_color": "FFD800",
        "trip_id": trip,
        "service_date": "20261022",
        "stop_id": "S1",
        "headsign": "Pestszentlőrinc",
        "scheduled_at": _iso(sched),
        "expected_at": _iso(exp),
        "minutes": 0,
        "realtime": realtime,
        "delay_sec": delay,
        "tracked": realtime,
        "canceled": False,
    }


def _payload():
    now = dt_util.utcnow().replace(microsecond=0)
    return {
        "api_version": 1,
        "server_time": _iso(now),
        "feed": {"id": "f1", "name": "Feed One", "short_name": None},
        "stop": {"stop_id": "S1", "stop_name": "Határ út M"},
        "departures": [
            _dep(
                "T1",
                now + timedelta(minutes=2, seconds=30),
                now + timedelta(minutes=4, seconds=30),
                realtime=True,
                delay=120,
            ),
            _dep(
                "T2",
                now + timedelta(minutes=9, seconds=30),
                now + timedelta(minutes=9, seconds=30),
                realtime=False,
                delay=None,
            ),
        ],
    }


STOP = {
    "stop_id": "S1",
    "stop_name": "Határ út M",
    "stop_desc": None,
    "distance_m": None,
    "is_station": False,
    "routes": [{"route_id": "R50", "route_label": "50", "direction": "Pestszentlőrinc"}],
}


def _mock_server(aioclient_mock):
    aioclient_mock.get(
        f"{BASE}/api/v1/info",
        json={
            "api_version": 1,
            "feeds": [{"id": "f1", "name": "Feed One"}, {"id": "f2", "name": "Feed Two"}],
        },
    )
    aioclient_mock.get(f"{BASE}/api/v1/feeds/f1/stops", json={"stops": [STOP]})
    aioclient_mock.get(
        f"{BASE}/api/v1/feeds/f1/stops/S1",
        json={"feed": {"id": "f1"}, "stop": STOP, "routes": STOP["routes"]},
    )
    aioclient_mock.get(f"{BASE}/api/v1/feeds/f1/stops/S1/departures", json=_payload())


def _calls(aioclient_mock, needle):
    return [c for c in aioclient_mock.mock_calls if needle in str(c[1])]


async def _add_stop(hass, *, query=None):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "transit.example.org/"}
    )
    assert result["step_id"] == "feed", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"feed_id": "f1"})
    assert result["step_id"] == "stop_search"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"query": query} if query else {}
    )
    assert result["step_id"] == "stop", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop_id": "S1"})
    assert result["step_id"] == "routes"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"route_ids": ["R50"], CONF_NAME: ""}
    )


async def test_full_flow_sensors_and_service(hass: HomeAssistant, aioclient_mock) -> None:
    hass.config.latitude, hass.config.longitude = 47.46, 19.12
    _mock_server(aioclient_mock)

    # Üres keresés → közeli megállók az otthon koordinátájával.
    result = await _add_stop(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Határ út M – 50"
    assert result["data"][CONF_URL] == BASE
    near = _calls(aioclient_mock, "/feeds/f1/stops?")[-1][1]
    assert near.query.get("lat") == "47.46" and near.query.get("radius_m") == "1000"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    dep_url = _calls(aioclient_mock, "departures")[-1][1]
    assert dep_url.query.get("route_id") == "R50"

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    print(sorted(states))
    nxt = states["sensor.hatar_ut_m_50_next_departure"]
    assert nxt.attributes["route"] == "50"
    assert nxt.attributes["delay_min"] == 2
    assert nxt.attributes["speech"].startswith(
        "Az 50-es villamos 4 perc múlva indul Pestszentlőrinc felé, 2 perc késéssel."
    ), nxt.attributes["speech"]
    assert states["sensor.hatar_ut_m_50_next_departure_minutes"].state == "4"
    table = states["sensor.hatar_ut_m_50_timetable"]
    assert table.state == "2"
    assert len(table.attributes["departures"]) == 2

    resp = await hass.services.async_call(
        DOMAIN,
        "get_departures",
        {"config_entry_id": entry.entry_id, "limit": 5},
        blocking=True,
        return_response=True,
    )
    assert resp["stop"] == "Határ út M"
    assert resp["departures"][1]["realtime"] is False
    assert "Utána 9 perc múlva jön a következő." in resp["speech"]

    # Ugyanaz a megálló ugyanazokkal a járatokkal másodszor nem vehető fel.
    result = await _add_stop(hass, query="Határ")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"

    # Options flow: elemszám + időablak + járat-szűrő → újratöltés az új paraméterekkel.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"route_ids": [], "departures": 5, "horizon_min": 60, "scan_interval": 45},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    dep_url = _calls(aioclient_mock, "departures")[-1][1]
    assert dep_url.query.get("limit") == "5" and "route_id" not in dep_url.query
    assert dep_url.query.get("horizon_min") == "60"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bad_server(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get("https://nope.example.org/api/v1/info", status=404)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://nope.example.org"}
    )
    assert result["errors"] == {"base": "invalid_api"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://x"}
    )
    assert result["errors"] == {"url": "invalid_url"}

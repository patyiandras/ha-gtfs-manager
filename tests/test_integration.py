"""Végig a varázslón, majd szenzorok és a `get_departures` szolgáltatás — mockolt szerverrel.

Futtatás: `pip install pytest-homeassistant-custom-component`, majd `pytest`.
"""

from __future__ import annotations

from datetime import timedelta
import json

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.gtfs_manager.const import DOMAIN

BASE = "https://transit.example.org"
LINE_50 = '["50", "Pestszentlőrinc"]'


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
    # MÁV-szerű eset: egy viszonylat több változata (route_id) — a választóban egyszer.
    "routes": [
        {"route_id": "R50", "route_label": "50", "direction": "Pestszentlőrinc"},
        {"route_id": "R50b", "route_label": "50", "direction": "Pestszentlőrinc"},
        {"route_id": "R50c", "route_label": "50", "direction": "Határ út M"},
    ],
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
    options = result["data_schema"].schema["lines"].config["options"]
    assert [o["label"] for o in options] == ["50 → Pestszentlőrinc", "50 → Határ út M"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lines": [LINE_50], CONF_NAME: ""}
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
    assert "route_id" not in dep_url.query
    assert json.loads(dep_url.query.get("lines")) == [["50", "Pestszentlőrinc"]]

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    print(sorted(states))
    nxt = states["sensor.hatar_ut_m_50_next_departure"]
    assert nxt.attributes["route"] == "50"
    assert nxt.attributes["delay_min"] == 2
    assert nxt.attributes["speech"].startswith(
        "Az 50-es villamos 4 perc múlva indul Pestszentlőrinc felé, 2 perc késéssel."
    ), nxt.attributes["speech"]
    assert states["sensor.hatar_ut_m_50_next_departure_minutes"].state == "4"
    # Késés: a következő indulás +2 perc; a 3 perces alapküszöb alatt → nincs riasztás.
    assert states["sensor.hatar_ut_m_50_delay"].state == "2"
    alert = hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert")
    assert alert.state == "off" and alert.attributes["speech"] == "Nincs jelentős késés."
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
        {"lines": [], "departures": 5, "horizon_min": 60, "scan_interval": 45},
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
    assert result["errors"] == {"base": "not_gtfs_manager"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://x"}
    )
    assert result["errors"] == {"url": "invalid_url"}


async def test_legacy_route_id_entry_preselects_its_line(hass: HomeAssistant, aioclient_mock) -> None:
    """Korábbi verzióval felvett (route_id-szűrős) bejegyzés: a beállításokban a
    viszonylata előre ki van jelölve, mentés után pedig viszonylatra szűr."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    _mock_server(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Határ út M – 50",
        data={
            CONF_URL: BASE,
            "feed_id": "f1",
            "feed_name": "Feed One",
            "stop_id": "S1",
            "stop_name": "Határ út M",
            "route_ids": ["R50b"],
            "route_labels": ["50"],
        },
        options={"departures": 10, "horizon_min": 180, "scan_interval": 60},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, "departures")[-1][1].query.get("route_id") == "R50b"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(k for k in result["data_schema"].schema if str(k) == "lines")
    assert key.default() == [LINE_50]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"lines": [LINE_50], "departures": 10, "horizon_min": 180, "scan_interval": 60},
    )
    await hass.async_block_till_done()
    assert entry.options["lines"] == [["50", "Pestszentlőrinc"]]
    assert entry.options["route_ids"] == []
    dep_url = _calls(aioclient_mock, "departures")[-1][1]
    assert "route_id" not in dep_url.query and "lines" in dep_url.query
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_delay_alert_threshold(hass: HomeAssistant, aioclient_mock) -> None:
    """A küszöb lejjebb vételével a +2 perces késés már riaszt, mondattal."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    _mock_server(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Határ út M – 50",
        data={
            CONF_URL: BASE,
            "feed_id": "f1",
            "feed_name": "Feed One",
            "stop_id": "S1",
            "stop_name": "Határ út M",
            "lines": [["50", "Pestszentlőrinc"]],
            "route_ids": [],
            "route_labels": ["50"],
        },
        options={"departures": 10, "horizon_min": 180, "scan_interval": 60, "delay_threshold": 2},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    alert = hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert")
    assert alert.state == "on"
    assert alert.attributes["delayed_count"] == 1
    assert alert.attributes["max_delay_min"] == 2
    assert alert.attributes["speech"].startswith(
        "Az 50-es villamos Pestszentlőrinc felé 2 perc késéssel indul,"
    ), alert.attributes["speech"]

    resp = await hass.services.async_call(
        DOMAIN,
        "get_departures",
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert len(resp["delayed"]) == 1 and resp["delay_speech"] == alert.attributes["speech"]

    # Élő adat nélküli menet késése ismeretlen, nem 0.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert any(str(k) == "delay_threshold" for k in result["data_schema"].schema)
    assert await hass.config_entries.async_unload(entry.entry_id)


def _entry(**extra):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        title="Határ út M – 50",
        data={
            CONF_URL: BASE,
            "feed_id": "f1",
            "feed_name": "Feed One",
            "stop_id": "S1",
            "stop_name": "Határ út M",
            "lines": [["50", "Pestszentlőrinc"]],
            "route_ids": [],
            "route_labels": ["50"],
        },
        options={"departures": 10, "horizon_min": 180, "scan_interval": 60},
        **extra,
    )


async def test_entity_ids_are_english_on_hungarian_ha(hass: HomeAssistant, aioclient_mock) -> None:
    """Magyar nyelvű HA-n is angol azonosító, magyar megjelenített névvel."""
    hass.config.language = "hu"
    _mock_server(aioclient_mock)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ids = sorted(s.entity_id for s in hass.states.async_all() if "hatar_ut_m_50" in s.entity_id)
    assert ids == [
        "binary_sensor.hatar_ut_m_50_delay_alert",
        "sensor.hatar_ut_m_50_delay",
        "sensor.hatar_ut_m_50_next_departure",
        "sensor.hatar_ut_m_50_next_departure_minutes",
        "sensor.hatar_ut_m_50_timetable",
    ], ids
    alert = hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert")
    assert alert.attributes["friendly_name"] == "Határ út M – 50 Késés van"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_legacy_hungarian_entity_ids_are_migrated(hass: HomeAssistant, aioclient_mock) -> None:
    """Korábbi verzió magyar azonosítói angolra; a kézzel átnevezett marad."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    _mock_server(aioclient_mock)
    entry = _entry()
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
    )
    ent_reg = er.async_get(hass)
    for domain, key, object_id in (
        ("sensor", "next_departure", "hatar_ut_m_50_kovetkezo_indulas"),
        ("binary_sensor", "delay_alert", "hatar_ut_m_50_keses_van"),
        ("sensor", "timetable", "sajat_menetrendem"),  # felhasználó nevezte át
    ):
        ent_reg.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            device_id=dev.id,
            suggested_object_id=object_id,
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.hatar_ut_m_50_next_departure") is not None
    assert hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert") is not None
    assert hass.states.get("sensor.hatar_ut_m_50_kovetkezo_indulas") is None
    assert hass.states.get("sensor.sajat_menetrendem") is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_delay_does_not_alert_but_later_delay_does(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A következő indulásról nincs élő adat (Delay: ismeretlen) → ettől nincs
    riasztás; egy KÉSŐBBI késő menet viszont bekapcsolja, okkal együtt."""
    now = dt_util.utcnow().replace(microsecond=0)
    no_rt = _dep("T1", now + timedelta(minutes=5), now + timedelta(minutes=5), realtime=False, delay=None)
    late = _dep(
        "T2", now + timedelta(minutes=30), now + timedelta(minutes=40), realtime=True, delay=600
    )
    payload = {"api_version": 1, "stop": {"stop_name": "Határ út M"}, "departures": [no_rt]}

    aioclient_mock.get(f"{BASE}/api/v1/feeds/f1/stops/S1/departures", json=payload)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    delay = hass.states.get("sensor.hatar_ut_m_50_delay")
    assert delay.state == "unknown" and "Nincs élő adat" in delay.attributes["reason"]
    assert hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert").state == "off"

    aioclient_mock.clear_requests()
    payload["departures"] = [no_rt, late]
    aioclient_mock.get(f"{BASE}/api/v1/feeds/f1/stops/S1/departures", json=payload)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hatar_ut_m_50_delay").state == "unknown"
    alert = hass.states.get("binary_sensor.hatar_ut_m_50_delay_alert")
    assert alert.state == "on"
    assert alert.attributes["reason"].startswith("50 ") and alert.attributes["reason"].endswith("→ +10 perc")
    assert await hass.config_entries.async_unload(entry.entry_id)

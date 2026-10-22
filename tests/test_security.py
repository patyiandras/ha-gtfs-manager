"""Bizalmi határ: a szerver válasza külső adat — átirányítás, méret, szemét-adat."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.gtfs_manager.api import (
    MAX_RESPONSE_BYTES,
    ApiInvalid,
    GtfsManagerApi,
    clean_departure,
    clean_text,
    normalize_url,
)
from custom_components.gtfs_manager.const import DOMAIN
from homeassistant.helpers.aiohttp_client import async_get_clientsession

BASE = "https://transit.example.org"


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_redirect_to_other_host_is_refused(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{BASE}/api/v1/info", status=302, headers={"Location": "http://192.168.1.1/api/v1/info"}
    )
    aioclient_mock.get("http://192.168.1.1/api/v1/info", json={"api_version": 1, "feeds": []})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE})
    assert result["errors"] == {"base": "redirect_other_host"}
    # A belső címre egyetlen kérés sem ment ki.
    assert not [c for c in aioclient_mock.mock_calls if "192.168.1.1" in str(c[1])]


async def test_same_host_redirect_is_followed_once(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        "http://transit.example.org/api/v1/info",
        status=301,
        headers={"Location": f"{BASE}/api/v1/info"},
    )
    aioclient_mock.get(
        f"{BASE}/api/v1/info", json={"api_version": 1, "feeds": [{"id": "f1", "name": "F"}]}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://transit.example.org"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "stop_search"


async def test_hostile_payload_is_sanitized(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{BASE}/api/v1/info",
        json={
            "api_version": 1,
            "feeds": [
                {"id": "../../etc", "name": "x"},
                {"id": {"a": 1}, "name": "obj"},
                {"id": "ok", "name": "Jó‮név\x00" + "a" * 500},
                "nem-dict",
            ],
        },
    )
    api = GtfsManagerApi(async_get_clientsession(hass), BASE)
    info = await api.info()
    assert [f["id"] for f in info["feeds"]] == ["../../etc", "ok"]
    name = info["feeds"][1]["name"]
    assert "‮" not in name and "\x00" not in name and len(name) == 200

    assert clean_departure({"route_id": "R", "trip_id": "T"}) is None  # időpont nélkül
    dep = clean_departure(
        {
            "route_id": "R",
            "trip_id": "T",
            "scheduled_at": "2026-10-22T10:00:00Z",
            "expected_at": "2026-10-22T10:00:00Z",
            "mode": "<script>",
            "route_color": "red;background:url(x)",
            "delay_sec": 10**12,
            "realtime": "yes",
        }
    )
    assert dep["mode"] == "unknown"
    assert dep["route_color"] is None
    assert dep["delay_sec"] is None
    assert dep["realtime"] is False
    assert dep["vehicle"] is None
    assert clean_text({"x": 1}) is None
    from custom_components.gtfs_manager.api import clean_position

    assert clean_position({"lat": 91, "lon": 19}) is None
    assert clean_position({"lat": "47", "lon": 19}) is None
    assert clean_position({"lat": 47.5, "lon": 19.1, "bearing": 999})["bearing"] is None


async def test_oversized_response_is_rejected(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/info", text="[" + "1," * MAX_RESPONSE_BYTES + "1]")
    api = GtfsManagerApi(async_get_clientsession(hass), BASE)
    try:
        await api.info()
    except ApiInvalid:
        pass
    else:  # pragma: no cover
        raise AssertionError("A túl nagy válasznak hibát kell dobnia")


def test_normalize_url() -> None:
    assert normalize_url("transit.example.org/") == "https://transit.example.org"
    assert normalize_url("https://x.org/sub/api/v1/info") == "https://x.org/sub"
    assert normalize_url("https://user:pw@x.org") is None
    assert normalize_url("file:///etc/passwd") is None
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("") is None
    # Bármelyik alak ugyanarra a címre vezet.
    for raw in (
        "menetrend.example.org",
        "https://menetrend.example.org",
        "https://menetrend.example.org/",
        "  HTTPS://Menetrend.Example.org/  ",
        "​menetrend.example.org​",
        "https://menetrend.example.org/api/v1/info",
    ):
        assert normalize_url(raw) == "https://menetrend.example.org", raw
    assert normalize_url("http://[::1]:3000") == "http://[::1]:3000"


async def test_prefilled_url_works_unchanged(hass: HomeAssistant, aioclient_mock) -> None:
    """Második megállónál az előtöltött cím (változtatás nélkül elküldve) is jó,
    és séma nélkül jelenik meg."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(domain=DOMAIN, data={CONF_URL: BASE, "stop_id": "X"}).add_to_hass(hass)
    aioclient_mock.get(
        f"{BASE}/api/v1/info", json={"api_version": 1, "feeds": [{"id": "f1", "name": "F"}]}
    )
    result = await _start(hass)
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_URL)
    suggested = key.description["suggested_value"]
    assert suggested == "transit.example.org"
    for value in (suggested, BASE, BASE + "/"):
        flow = await _start(hass)
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_URL: value})
        assert flow["step_id"] == "stop_search", (value, flow)


async def test_options_keep_route_filter_when_server_unreachable(
    hass: HomeAssistant, aioclient_mock
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    aioclient_mock.get(
        f"{BASE}/api/v1/feeds/f1/stops/S1/departures",
        json={"stop": {"stop_name": "S"}, "departures": []},
    )
    aioclient_mock.get(f"{BASE}/api/v1/feeds/f1/stops/S1", status=500)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="S – 50",
        data={
            CONF_URL: BASE,
            "feed_id": "f1",
            "feed_name": "F",
            "stop_id": "S1",
            "stop_name": "S",
            "route_ids": ["R50"],
            "route_labels": ["50"],
        },
        options={"departures": 10, "horizon_min": 180, "scan_interval": 60},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "route_ids" not in {str(k) for k in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"departures": 5, "horizon_min": 60, "scan_interval": 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["route_ids"] == ["R50"]
    await hass.async_block_till_done()


async def test_server_errors_are_reported_precisely(hass: HomeAssistant, aioclient_mock) -> None:
    """Karbantartás / korlát / egyéb HTTP-hiba nem „rossz cím"-ként jelenik meg."""
    for status, expected in ((503, "maintenance"), (429, "rate_limited"), (502, "http_error"), (404, "not_gtfs_manager")):
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE}/api/v1/info", status=status)
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE})
        assert result["errors"] == {"base": expected}, (status, result["errors"])
        detail = result["description_placeholders"]["status"]
        if expected in ("http_error", "not_gtfs_manager"):
            assert f"{BASE}/api/v1/info → HTTP {status}" in detail, detail
            assert "nem Cloudflare" in detail  # a mock nem küld CF-RAY fejlécet


async def test_url_with_path_falls_back_to_root(hass: HomeAssistant, aioclient_mock) -> None:
    """A böngészőből bemásolt `…/home` címen a weboldal válaszol — a gyökér a jó."""
    aioclient_mock.get(f"{BASE}/home/api/v1/info", text="<html>app</html>")
    aioclient_mock.get(
        f"{BASE}/api/v1/info", json={"api_version": 1, "feeds": [{"id": "f1", "name": "F"}]}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{BASE}/home"}
    )
    assert result["step_id"] == "stop_search", result

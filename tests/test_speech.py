"""A felolvasó-szöveg tesztjei — HA nélkül futnak: `python tests/test_speech.py`."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "speech", Path(__file__).parents[1] / "custom_components" / "gtfs_manager" / "speech.py"
)
speech = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(speech)


def dep(label, mode, minutes, *, headsign=None, delay=None, rt=True, canceled=False, clock="14:05"):
    return {
        "route_label": label,
        "mode": mode,
        "headsign": headsign,
        "minutes": minutes,
        "clock": clock,
        "delay_sec": delay,
        "realtime": rt,
        "canceled": canceled,
    }


def test_suffix():
    cases = {1: "es", 2: "es", 3: "as", 4: "es", 5: "ös", 6: "os", 7: "es", 8: "as", 9: "es",
             10: "es", 20: "as", 30: "as", 40: "es", 50: "es", 60: "as", 70: "es", 80: "as",
             90: "es", 100: "as", 200: "as", 1000: "es", 47: "es", 133: "as", 260: "as"}
    for n, s in cases.items():
        assert speech.number_suffix(n) == s, (n, speech.number_suffix(n), s)


def test_article():
    assert speech.article("50") == "az"
    assert speech.article("5") == "az"
    assert speech.article("1") == "az"
    assert speech.article("3") == "a"
    assert speech.article("15") == "a"
    assert speech.article("100") == "a"
    assert speech.article("1000") == "az"
    assert speech.article("M3") == "az"
    assert speech.article("H5") == "a"
    assert speech.article("S60") == "az"
    assert speech.article("Sikló") == "a"
    assert speech.article("IC 705") == "az"


def test_route_phrase():
    assert speech.route_phrase("50", "tram") == "az 50-es villamos"
    assert speech.route_phrase("3", "bus") == "a 3-as busz"
    assert speech.route_phrase("M3", "subway") == "az M3-as metró"
    assert speech.route_phrase("H5", "suburban") == "a H5-ös HÉV"
    assert speech.route_phrase("4-6", "tram") == "a 4-6-os villamos"
    assert speech.route_phrase("NAPFÉNY", "rail") == "a NAPFÉNY vonat"
    assert speech.route_phrase("100E", "bus") == "a 100E busz"


def test_build_speech():
    s = speech.build_speech(
        [dep("50", "tram", 3, headsign="Pestszentlőrinc, Béke tér", delay=120),
         dep("50", "tram", 8)]
    )
    assert s == (
        "Az 50-es villamos 3 perc múlva indul Pestszentlőrinc, Béke tér felé, 2 perc késéssel. "
        "Utána 8 perc múlva jön a következő."
    ), s
    s = speech.build_speech([dep("3", "bus", 0, rt=False), dep("52", "bus", 5)])
    assert s == "A 3-as busz most indul, menetrend szerint. Utána az 52-es busz 5 perc múlva.", s
    s = speech.build_speech([dep("50", "tram", 75, clock="15:20", delay=-90)])
    assert s == "Az 50-es villamos 15 óra 20 perckor indul, 2 perccel korábban.", s
    s = speech.build_speech([dep("50", "tram", 2, canceled=True)], stop_name="Határ út M", horizon_min=60)
    assert s == "Határ út M megállóból a következő 60 percben nincs indulás.", s



def test_delay_speech():
    deps = [
        dep("50", "tram", 7, headsign="Pestszentlőrinc", delay=300, clock="15:36"),
        dep("50", "tram", 9, delay=60, clock="15:38"),
        dep("3", "bus", 12, canceled=True, clock="15:40") | {"sched_clock": "15:40"},
        dep("52", "bus", 20, rt=False, delay=None, clock="15:50"),
    ]
    s = speech.build_delay_speech(deps, threshold_min=3)
    assert s == (
        "Az 50-es villamos Pestszentlőrinc felé 5 perc késéssel indul, 15 óra 36 perckor. "
        "A 3-as busz 15 óra 40 perces menete elmarad."
    ), s
    assert speech.build_delay_speech(deps[1:2] + deps[3:], threshold_min=3) == "Nincs jelentős késés."
    assert speech.is_delayed(deps[1], 1) and not speech.is_delayed(deps[1], 2)
    assert speech.at_clock("15:00") == "15 órakor"
    assert speech.clock_adjective("15:00") == "15 órás"
    # Sietés nem késés.
    assert not speech.is_delayed(dep("50", "tram", 1, delay=-300), 1)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")

"""Magyar nyelvű, felolvasható mondat a következő indulásokról.

Szándékosan Home Assistant-független (csak sima dict-eket kap), hogy önállóan
tesztelhető legyen. A bemeneti elemek kulcsai:

    route_label  str   — „50", „M3", „H5", „IC 705 NAPFÉNY"
    mode         str   — tram / bus / subway / rail / suburban / coach /
                         trolleybus / ferry / other / unknown
    headsign     str|None
    minutes      int   — egész perc a várható indulásig
    clock        str   — a várható indulás helyi ideje, „HH:MM"
    delay_sec    int|None
    realtime     bool
    canceled     bool
"""

from __future__ import annotations

import re
from typing import Any

MODE_NOUN = {
    "tram": "villamos",
    "subway": "metró",
    "rail": "vonat",
    "suburban": "HÉV",
    "bus": "busz",
    "coach": "busz",
    "trolleybus": "troli",
    "ferry": "hajó",
}

_VOWELS = "aáeéiíoóöőuúüű"
# Betűk, amelyeknek a NEVE magánhangzóval kezdődik (ef, el, em, en, er, es, iksz, ipszilon).
_VOWEL_NAMED_LETTERS = "flmnrsxy"

# Egyes helyiérték (1–9) és tízes (10–90) szerinti toldalék.
_UNIT_SUFFIX = {1: "es", 2: "es", 3: "as", 4: "es", 5: "ös", 6: "os", 7: "es", 8: "as", 9: "es"}
_TENS_SUFFIX = {1: "es", 2: "as", 3: "as", 4: "es", 5: "es", 6: "as", 7: "es", 8: "as", 9: "es"}


def number_suffix(n: int) -> str:
    """A szám -s képzős alakjának toldaléka: 3 → „as", 50 → „es", 100 → „as"."""
    if n == 0:
        return "ás"
    if n % 10:
        return _UNIT_SUFFIX[n % 10]
    if n % 100:
        return _TENS_SUFFIX[(n // 10) % 10]
    if n % 1000:
        return "as"  # száz, kétszáz …
    if n % 1_000_000:
        return "es"  # ezer
    return "os"  # millió


def _number_starts_with_vowel(n: int) -> bool:
    """A szám KIOLVASVA magánhangzóval kezdődik-e (egy, öt, ötven, ezer …)."""
    s = str(n)
    first = int(s[0])
    if len(s) == 1:
        return first in (1, 5)
    if len(s) in (2, 3):
        return first == 5  # tíz/száz igen, „ötven", „ötszáz"
    if len(s) == 4:
        return first in (1, 5)  # „ezer", „ötezer"
    return False


def article(label: str) -> str:
    """Határozott névelő a kiolvasott alakhoz: „a" vagy „az"."""
    text = label.strip()
    if not text:
        return "a"
    m = re.match(r"\d+", text)
    if m:
        return "az" if _number_starts_with_vowel(int(m.group())) else "a"
    first = text[0].lower()
    if first in _VOWELS:
        return "az"
    # Rövidítés (betű + szám, pl. „M3", „S60"): a betű NEVÉT mondjuk ki.
    if re.match(r"^[A-ZÁÉÍÓÖŐÚÜŰ]{1,2}\s?\d", text) and first in _VOWEL_NAMED_LETTERS:
        return "az"
    return "a"


def route_phrase(label: str, mode: str | None) -> str:
    """„az 50-es villamos", „a 3-as busz", „az M3-as metró", „a NAPFÉNY vonat"."""
    text = (label or "").strip()
    noun = MODE_NOUN.get(mode or "", "járat")
    m = re.search(r"(\d+)$", text)
    if m:
        body = f"{text}-{number_suffix(int(m.group(1)))}"
    else:
        body = text
    if noun.lower() in body.lower():
        return f"{article(text)} {body}".strip()
    return f"{article(text)} {body} {noun}".strip()


def when_phrase(minutes: int, clock: str) -> str:
    if minutes <= 0:
        return "most"
    if minutes < 60:
        return f"{minutes} perc múlva"
    hh, _, mm = clock.partition(":")
    h, mi = int(hh), int(mm or 0)
    return f"{h} óra {mi} perckor" if mi else f"{h} órakor"


def delay_phrase(dep: dict[str, Any]) -> str:
    if not dep.get("realtime"):
        return ", menetrend szerint"
    delay = dep.get("delay_sec") or 0
    mins = round(abs(delay) / 60)
    if mins == 0:
        return ""
    if delay > 0:
        return f", {mins} perc késéssel"
    return f", {mins} perccel korábban"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def build_speech(
    departures: list[dict[str, Any]],
    stop_name: str | None = None,
    horizon_min: int | None = None,
    max_items: int = 2,
) -> str:
    """Pl. „Az 50-es villamos 3 perc múlva indul Pestszentlőrinc felé, 2 perc
    késéssel. Utána 8 perc múlva jön a következő."
    """
    active = [d for d in departures if not d.get("canceled")]
    if not active:
        where = f"{stop_name} megállóból " if stop_name else ""
        span = f"a következő {horizon_min} percben " if horizon_min else ""
        return _cap(f"{where}{span}nincs indulás.")

    first = active[0]
    toward = f" {first['headsign']} felé" if first.get("headsign") else ""
    parts = [
        _cap(
            f"{route_phrase(first.get('route_label', ''), first.get('mode'))} "
            f"{when_phrase(int(first.get('minutes', 0)), first.get('clock', ''))} "
            f"indul{toward}{delay_phrase(first)}."
        )
    ]
    for dep in active[1:max_items]:
        same_route = dep.get("route_label") == first.get("route_label")
        when = when_phrase(int(dep.get("minutes", 0)), dep.get("clock", ""))
        if same_route:
            parts.append(f"Utána {when} jön a következő.")
        else:
            parts.append(
                f"Utána {route_phrase(dep.get('route_label', ''), dep.get('mode'))} {when}."
            )
    return " ".join(parts)


def is_delayed(dep: dict[str, Any], threshold_min: int) -> bool:
    """Élő adat szerint legalább `threshold_min` percet késik (sietés nem számít)."""
    delay = dep.get("delay_sec")
    return bool(dep.get("realtime")) and delay is not None and delay >= threshold_min * 60


def _clock_parts(clock: str) -> tuple[int, int] | None:
    hh, _, mm = (clock or "").partition(":")
    if not hh.isdigit() or not (mm or "0").isdigit():
        return None
    return int(hh), int(mm or 0)


def at_clock(clock: str) -> str:
    """„15:36" → „15 óra 36 perckor", „15:00" → „15 órakor"."""
    p = _clock_parts(clock)
    if p is None:
        return clock
    return f"{p[0]} óra {p[1]} perckor" if p[1] else f"{p[0]} órakor"


def clock_adjective(clock: str) -> str:
    """„15:40" → „15 óra 40 perces", „15:00" → „15 órás" (a menete elé)."""
    p = _clock_parts(clock)
    if p is None:
        return clock
    return f"{p[0]} óra {p[1]} perces" if p[1] else f"{p[0]} órás"


def build_delay_speech(
    departures: list[dict[str, Any]], threshold_min: int, max_items: int = 3
) -> str:
    """Pl. „Az 50-es villamos Pestszentlőrinc felé 5 perc késéssel indul, 15 óra
    36 perckor. A 3-as busz 15 óra 40 perces menete elmarad."

    A bemenet ugyanaz, mint a `build_speech`-é, plusz `sched_clock` (a
    menetrendi indulás „HH:MM"). Ha nincs késés vagy elmaradó menet:
    „Nincs jelentős késés."
    """
    hits = [d for d in departures if d.get("canceled") or is_delayed(d, threshold_min)]
    if not hits:
        return "Nincs jelentős késés."
    parts: list[str] = []
    for dep in hits[:max_items]:
        phrase = route_phrase(dep.get("route_label", ""), dep.get("mode"))
        if dep.get("canceled"):
            sched = clock_adjective(dep.get("sched_clock") or dep.get("clock", ""))
            parts.append(_cap(f"{phrase} {sched} menete elmarad."))
            continue
        toward = f" {dep['headsign']} felé" if dep.get("headsign") else ""
        mins = round((dep.get("delay_sec") or 0) / 60)
        parts.append(
            _cap(f"{phrase}{toward} {mins} perc késéssel indul, {at_clock(dep.get('clock', ''))}.")
        )
    rest = len(hits) - max_items
    if rest > 0:
        parts.append(f"Továbbá még {rest} járat késik vagy elmarad.")
    return " ".join(parts)

"""Konstansok a GTFS Manager integrációhoz."""

from __future__ import annotations

DOMAIN = "gtfs_manager"

CONF_FEED_ID = "feed_id"
CONF_FEED_NAME = "feed_name"
CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
CONF_ROUTE_IDS = "route_ids"
CONF_ROUTE_LABELS = "route_labels"
# Viszonylatok: [[járatnév, irány|None], …] — a `route_id`-k a szerveren oldódnak fel.
CONF_LINES = "lines"

CONF_DEPARTURES = "departures"
CONF_HORIZON = "horizon_min"
CONF_SCAN_INTERVAL = "scan_interval"
# Ennyi perc késéstől számít „késésnek" a Késés van bináris szenzornál.
CONF_DELAY_THRESHOLD = "delay_threshold"

DEFAULT_DEPARTURES = 10
DEFAULT_HORIZON = 180
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600
DEFAULT_DELAY_THRESHOLD = 3
MAX_DELAY_THRESHOLD = 60

# A szerver ennyit enged egy kérésben (lásd a szerver `/api/v1` dokumentációját).
MAX_DEPARTURES = 50
MAX_HORIZON = 720

# Közeli megállók keresési sugara (m), ha a felhasználó nem ír be nevet.
NEARBY_RADIUS_M = 1000

SERVICE_GET_DEPARTURES = "get_departures"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_LIMIT = "limit"

MODE_ICONS = {
    "tram": "mdi:tram",
    "subway": "mdi:subway-variant",
    "rail": "mdi:train",
    "suburban": "mdi:train-variant",
    "bus": "mdi:bus",
    "coach": "mdi:bus-side",
    "trolleybus": "mdi:bus-electric",
    "ferry": "mdi:ferry",
}
DEFAULT_ICON = "mdi:bus-clock"

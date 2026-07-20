"""Constants for the Insite Energy integration."""
from __future__ import annotations

DOMAIN = "insite_energy"
BASE_URL = "https://my.insite-energy.co.uk"
LOGIN_URL = f"{BASE_URL}/Account/Login"

CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 12

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

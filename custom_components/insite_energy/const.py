"""Constants for the Insite Energy integration."""
from __future__ import annotations

DOMAIN = "insite_energy"
BASE_URL = "https://my.insite-energy.co.uk"
LOGIN_PATH = "/Account/Login"
DETAILS_PATH = "/Customer/Details"
LOGIN_URL = f"{BASE_URL}{LOGIN_PATH}"
DETAILS_URL = f"{BASE_URL}{DETAILS_PATH}"

# The account payload is embedded in the page as a JS object literal.
VIEW_MODEL_MARKER = "var viewModel = "

CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 12
MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL = 168

# Key used to stash the poll timestamp inside the viewModel dict.
LAST_POLL_KEY = "_last_poll_time"

# On-disk cache of the last successful response, so entities can be restored
# at startup without waiting for the (slow) website.
STORAGE_VERSION = 1
CACHE_SAVE_DELAY = 10

# The site regularly takes tens of seconds to respond.
REQUEST_TIMEOUT = 90

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

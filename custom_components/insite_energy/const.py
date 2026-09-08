"""Constants for the Insite Energy integration."""
from __future__ import annotations

DOMAIN = "insite_energy"
BASE_URL = "https://my.insite-energy.co.uk"
LOGIN_PATH = "/Account/Login"
DETAILS_PATH = "/Customer/Details"
LOGIN_URL = f"{BASE_URL}{LOGIN_PATH}"
DETAILS_URL = f"{BASE_URL}{DETAILS_PATH}"

# Two-factor authentication. The portal emails a six digit code and offers to
# remember the browser for 45 days; the login POST only ever redirects to the
# verify page, so the code is requested by walking the same steps the site's own
# login script walks.
TWO_FACTOR_PATH = "/TwoFactorAuthentication/VerifyTwoFactorAuthentication"
TWO_FACTOR_URL = f"{BASE_URL}{TWO_FACTOR_PATH}"
VALIDATE_ACCOUNT_URL = f"{BASE_URL}/Account/ValidateUserRoleAndConsumerStatus"
CHECK_TWO_FACTOR_URL = f"{BASE_URL}/Account/CheckIsTwoFactorRequired"
REGISTRATION_LOGIN_URL = f"{BASE_URL}/Account/RegistrationLogin"
RESEND_ENABLE_URL = f"{BASE_URL}/Account/EnableDisableResend2FACode"
RESEND_CODE_URL = f"{BASE_URL}/TwoFactorAuthentication/ResendTwoFactorVerification"

# The account payload is embedded in the page as a JS object literal.
VIEW_MODEL_MARKER = "var viewModel = "

CONF_CODE = "code"
CONF_RESEND = "resend"

CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 12
MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL = 168

# Per-utility weighting for the spread statistics, keyed on utility_key(). Each
# value is a single statistic id whose long-term statistics say when that
# utility was in use; a missing entry spreads its readings evenly instead.
CONF_WEIGHTS = "weights"

# Suffixes for the external statistics we publish, e.g. "insite_energy:hh_energy".
STAT_ENERGY_SUFFIX = "energy"
STAT_COST_SUFFIX = "cost"

# Keys used to stash our own metadata inside the viewModel dict.
LAST_POLL_KEY = "_last_poll_time"
CACHE_ACCOUNT_KEY = "_cached_for_username"

# On-disk cache of the last successful response, so entities can be restored
# at startup without waiting for the (slow) website.
STORAGE_VERSION = 1

# The cookie that remembers a verified browser lasts 45 days, so it has to
# outlive the aiohttp session it arrived on - which a restart, a reload and an
# options change all replace. Without somewhere durable to keep it, every one of
# those costs the user another emailed code.
#
# It lives in entry.data rather than the response cache because the config flow
# is what first obtains it, and entry.data is the only store that exists before
# the entry does. A cache keyed on entry id could not be written until after
# setup, by which time the first poll has already asked for a second code.
CONF_COOKIES = "cookies"

# The site regularly takes tens of seconds to respond.
REQUEST_TIMEOUT = 90

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

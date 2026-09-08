# Architecture

A Home Assistant custom integration (distributed via HACS) that tracks account balance,
meter readings and tariffs from Insite Energy heat networks. See the [README](README.md)
for what it does from a user's point of view.

There is no API. The account payload is a JavaScript object literal embedded in the HTML
of the customer portal's `/Customer/Details` page, so the integration logs in, fetches
that page, and extracts the `viewModel` from it.

Requires Home Assistant 2025.11.0+ — the spread statistics set `unit_class` on their
metadata, which recorder only gained in that release.

## Shape

Everything lives in `custom_components/insite_energy/`:

```
api.py ──> coordinator.py ──┬──> sensor.py       (entities)
                            └──> statistics.py   (external statistics)
config_flow.py (setup / reauth / options)
util.py        (parsing and identity helpers, shared by all of the above)
```

### api.py

`InsiteClient` talks to the portal. Logging in takes several seconds and occasionally
over thirty; the details page serves the same payload in under a second once the session
cookie is set. That cookie lasts about twenty minutes and does not slide, so every poll
tries the cheap request first and falls back to a full login.

Session reuse is also a correctness matter, not just a speed one: while the cookie is
valid the site redirects the login page to the details page, so a blind re-login finds no
CSRF token and fails.

The client distinguishes two failure classes, and the distinction is load-bearing:

- `InsiteAuthError` — genuinely bad credentials. The coordinator turns this into
  `ConfigEntryAuthFailed`, which prompts the user to reauthenticate **and stops
  scheduling polls**.
- `InsiteApiError` — anything else. Retried on the normal interval.

A failed login re-renders the login page, but so do a lockout and a maintenance window.
Misreading one of those as a bad password stops polling until the user re-enters a
password that was correct all along, so `_TRANSIENT_LOGIN_RE` reclassifies them. It is
matched against the page with its scripts stripped: every render carries
`var genericError = '...Please try again later.'`, which otherwise matches every time and
leaves `InsiteAuthError` unreachable.

### Two-factor authentication

The portal can require a six digit code, emailed on sign-in. A verified browser is
remembered for 45 days by a cookie, `insite_2fa_rb`, set on the verify response. While
that cookie is presented, the ordinary login POST lands straight on the details page and
no code is asked for; once it is gone, the login POST lands on the code form instead.

**The cookie must be sent to the portal exactly as issued — unquoted.** `insite_2fa_rb`
is a base64 blob, so its value carries `/`, `+` and `=`. aiohttp's default cookie jar
treats those as needing quoting and sends `insite_2fa_rb="…"`; the portal compares the
raw token, so the quoted form matches nothing and is read as no token at all — which is
why storing the cookie faithfully still drew a fresh code on every poll until the jar was
built with `quote_cookie=False` (`strict_cookie_jar`). This is the whole of what keeps a
verified browser verified; a browser gets it right for free, which is why the site works
in one but not through the default client.

A poll therefore does nothing special: it posts the login form, and a valid
`insite_2fa_rb` carries it to the details page. When the cookie is absent or expired the
POST lands on the code form, recognised by its markup as well as its path (so a moved form
still routes to a prompt rather than "unexpected page"). That is `InsiteTwoFactorRequired`,
an `InsiteAuthError` subclass, so polling stops and the user is asked — retrying could only
reach the same form. **The poll path never calls the check endpoint**: that endpoint emails
a code whenever it reports `RequiresVerification`, so calling it on a poll would be a code
per poll.

**Getting a code sent** is the reauth flow's job, and it runs what the site's own login
script runs: fetch the login page for its key, `/Account/ValidateUserRoleAndConsumerStatus`
(the only step that names a refusal, so a wrong password stays a wrong password rather than
a failure to connect), `/Account/CheckIsTwoFactorRequired`, then the verify page fetched
with `isSubsequent=true`. Which of the check and that page sends the email is not
observable from outside; the browser performs both and so does this, so it need not be
known.

Each page carries a per-render `base64AuthKey` that its JSON endpoints want echoed back as
HTTP Basic credentials, and the keys are not interchangeable between renders, so every page
is scraped for its own. The verify call is multipart with the address percent-encoded,
matching what the site's script builds; a urlencoded body with a plain address is a
different request. After a code is accepted, `/Account/RegistrationLogin` is what the site
treats as the point the session becomes usable, so it is called before the details page.

The remembered-browser cookie is kept in `entry.data`, not in the response cache: the
config flow is what first obtains it, and `entry.data` is the only store that exists before
the entry does. A cache keyed on entry id could not be written until after setup, by which
time the first poll has already asked for a second code. Changing the email in the options
flow drops it, for the same reason `_cached_for_username` exists.

The whole reauth exchange runs on one session held by the config flow. The CSRF token, the
page key and the partial sign-in a correct password earns are all bound to it, so a code
submitted on a fresh session answers a verification that never started.

### coordinator.py

`InsiteEnergyDataUpdateCoordinator` owns two things worth knowing about.

**A dedicated aiohttp session.** Cookie reuse is what makes the warm path work, and
sharing Home Assistant's session would cross-contaminate cookies. It is created during
entry setup so Home Assistant detaches it on unload; closing it here is forbidden.

**An on-disk cache of the last successful response.** Setup serves that cache
immediately and refreshes in the background, so a slow login never blocks Home Assistant
startup. The cache is keyed on the config entry id, which survives an email change in the
options flow, so a `_cached_for_username` guard stops the previous account's balance and
readings being served as current.

### sensor.py

Entities are built from whatever snapshot the coordinator currently holds — at startup
that is the cache, not a live poll. A coordinator listener therefore adds sensors for
utilities that only appear once the first live refresh lands.

### config_flow.py

User, reauth and options steps. The options flow calls
`async_schedule_reload` explicitly and there is deliberately **no** update listener: a
listener fired on any entry change, which made a reauth reload twice over and a rename
reload for nothing, and Home Assistant warns that the combination stops working in
2026.12.

## Spread statistics

The substantial part of the integration, in `statistics.py`.

Readings arrive long after the fact, covering everything since the previous reading — a
month at a time for some utilities. Home Assistant attributes the whole delta to the hour
the reading happened to arrive, so the Energy dashboard shows a month of usage as a single
spike.

`MeterReadingDate` says which period a reading actually covers, so that consumption is
re-published spread across the hours it belongs to, as **external** statistics:

| Statistic | Unit |
| --- | --- |
| `insite_energy:<utility>_energy` | kWh |
| `insite_energy:<utility>_cost` | GBP |

They have to be external rather than the entity's own. The period is over by the time we
learn of it, so there is no live value a sensor could report, and the recorder would
overwrite anything written to an entity-backed statistic.

Users add these to the Energy dashboard **instead of** the Meter Reading entity.

### Weighting

A spread is even by default. If a utility has an activity signal configured — one
statistic id, chosen in the options flow — the spread follows it instead, so energy lands
on the hours it was really used. A `measurement` statistic contributes its hourly mean; a
cumulative one contributes the rise across each hour, which is what lets a meter be used
directly rather than its flow sensor.

The `StatisticSelector` cannot be filtered, so it also offers this integration's own
output. `_is_own_output` refuses both spellings — the `insite_energy:` statistics and the
`sensor.` meter readings this integration registered — and falls back to an even spread
with a warning.

### Refusals

Publishing the wrong thing is worse than publishing nothing, because a statistic is
awkward to correct once written. Each of these is refused, with a warning saying which
fired: the meter went backwards; the reading is dated in the future; the window is longer
than `MAX_WINDOW_HOURS`; the window overlaps hours already published.

## Design decisions that constrain change

These look like redundancy or bugs and are neither. The code comments carry the full
reasoning.

**A 2FA challenge in the options flow is a passing credential check.** Reaching the code
form means the portal accepted the password, which is all that check establishes. The code
itself is asked for by the reauth prompt the next poll raises, rather than by growing a
second verification step into the options flow.

**The client's cookie jar is built with `quote_cookie=False`.** The remembered-browser
token contains base64 punctuation (`/`, `+`); the default jar quotes such values on send,
and the portal then matches nothing and demands a fresh code every poll. Restoring the
default re-breaks two-factor persistence outright, with nothing in the stored cookie to
show for it. See `strict_cookie_jar`.

**Statistics must never cost a poll.** `async_publish_spread_statistics` swallows every
failure — a recorder-less instance, one malformed utility, anything unforeseen. The
entities are the point of the integration and work fine without statistics.

**The startup re-announcement is deliberate.** The Energy dashboard's cost sensor accrues
nothing on the first meter event it sees: it takes that reading as a baseline and returns.
It only ever initialises from a `state_changed` event, and it registers its listener after
this integration's entities already exist. Re-announcing the cached reading gives it a
harmless baseline — with `force_update` on the reading sensor, since Home Assistant would
otherwise collapse an unchanged write into a `state_reported` event the cost sensor does
not listen for. Doing this strictly *before* the refresh is what makes a meter that moved
while Home Assistant was down still get charged.

**`utility_key()` is baked into entity unique ids and device identifiers.** Changing it
orphans whatever an existing install has registered. Slugging for the recorder's
`[\da-z_]+:[\da-z_]+` rule happens separately, in `statistic_id()`, for exactly that
reason.

**Recorder emits epoch _seconds_.** Only the websocket API scales to milliseconds, for the
frontend. Reading these as milliseconds lands every hour in 1970, matches nothing, and
silently degrades weighting to an even spread.

**The cache is written synchronously, not debounced.** A delayed write outlives the thing
that scheduled it: deleting the entry removes the file and the pending write puts it
straight back, and a reload inside the delay leaves the new coordinator reading the
previous poll off disk — which then derives a window overlapping one already published.
Polls are hours apart, so there is nothing to debounce.

**`_hours_between` floors both ends, not just the start.** Flooring only the start makes
consecutive windows share an hour whenever a reading's UTC instant is not hour-aligned.
The overlap guard reads that shared row as a partial overlap and refuses the window, so
every other reading is silently dropped and never retried.

# AGENTS.md

Working notes for coding agents. Read [ARCHITECTURE.md](ARCHITECTURE.md) first — it
explains how the integration is put together and, importantly, which parts look redundant
but are not. The [README](README.md) describes the user-facing behaviour, including what
each entity means and why a reading can be refused.

@./ARCHITECTURE.md

## Commands

Python 3.14 is required: `pytest-homeassistant-custom-component` needs it, and on 3.13 pip
silently resolves back to a release six months old instead of failing.

```bash
pip install -r requirements_test.txt
pytest                                          # asyncio_mode=auto, testpaths=tests
pytest tests/test_statistics.py::test_name -q   # single test
ruff check .                                    # rule selection is in ruff.toml
```

Installing the test requirements pulls in Home Assistant itself and is slow. To lint only,
install just the pinned ruff:

```bash
python3 -m venv .venv && .venv/bin/pip install $(grep '^ruff==' requirements_test.txt)
```

CI (`.github/workflows/tests.yml`) runs `pytest` with `--cov-fail-under=90` plus
`ruff check`, and separately validates with hassfest and HACS.

## Conventions

**Do not run `ruff format`.** The code is not black-formatted and reformatting would bury
real changes in whitespace. Rule selection in `ruff.toml` is explicit rather than
inherited, so that a ruff release cannot silently change what CI means.

**Dependencies are pinned on purpose.** `pytest-homeassistant-custom-component` drags in
Home Assistant, so an unpinned version means every CI run tests a different Home
Assistant. A scheduled `latest` job installs it unpinned instead, so upstream breakage
surfaces on its own timetable rather than mid-PR.

**`strings.json` and `translations/en.json` are a pair.** The former uses
`[%key:component::insite_energy::...%]` references; the latter has them resolved. Editing
only `strings.json` changes nothing users see.

**Read the comments before simplifying.** Much of this code carries non-obvious *why* in
comments directly above it — see the design decisions section of ARCHITECTURE.md for the
short list of things that will otherwise look like safe cleanups.

## Tests

Fixtures in `tests/conftest.py`: `view_model` (a trimmed real payload), `config_entry`, and
`mock_client`, which patches `InsiteClient` in both the coordinator and the config flow and
deep-copies each response — otherwise the coordinator's previous snapshot *is* the new
payload, and anything comparing the two sees no change.

`tests/test_statistics_recorder.py` shadows that autouse fixture for its own module,
because `recorder_mock` refuses to build a database once `hass` exists. It is the only
weighting test that goes through a real recorder, and it exists because fixture-based
tests can agree with the code on a wrong unit and all still pass — which is how the
epoch-seconds bug survived.

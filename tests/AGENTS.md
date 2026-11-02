# Tests

## Overview

- Test suite uses the standard-library `unittest` runner.
- Canonical command: `python -m unittest discover -v tests` (about 1,690 tests, ~30s; offline, no services).
- Integration command: `python -m unittest discover -v -s tests/integration` against a disposable MongoDB
  replica set, with `FINAL_TEST_MONGO_URI` and `INTEGRATION_MONGO_URI` set. CI's `integration` job starts one
  with docker; a missing URI must fail, never skip.
- Async cases subclass `unittest.IsolatedAsyncioTestCase`.
- Unit tests isolate cogs with mocks; only `tests/integration/` needs a database.

## Where to look

| File | Coverage focus |
| --- | --- |
| `test_match_commands.py` | Match queueing, pairing, base-store reservations, slot sync; legacy broad suite |
| `test_match_*.py`, `test_queued_district_selection.py` | Focused match lifecycle, publication and queue flows |
| `test_base_store_*.py`, `test_live_base_*.py`, `test_raid_*.py` | Stored-base reservation lifecycle and raid auto-store |
| `test_result_*.py`, `test_autowin_*.py`, `test_*_reset_*.py` | Result operations, retries, autowins and resets |
| `test_self_*.py`, `test_helper_*.py` | Self-hosting and Helper API flows |
| `test_base_fingerprint_*.py`, `test_base_comparison*.py`, `*_inference.py` | Base finder and fingerprint inference |
| `_<topic>_fixture.py`, `_result_retry_store.py` | Shared fixtures; the only modules tests may import from |
| `test_base_commands.py` | Base add/info/remove/renew, metadata, quality, builder, leaderboard |
| `test_player_commands.py` | Registration, profiles, match history, stats, hit rates, selectors |
| `test_thread_commands.py` | Thread deletion and match/base-store cleanup |
| `test_embed_commands.py` | Clan availability embed rendering |
| `test_autocomplete_error_handler.py` | Interaction availability and fallback HTTP errors |
| `cogs/` | Several cogs remain untested; add focused coverage when changing them |

## Test conventions

- Build bot, context, user, query, and model doubles with `MagicMock`.
- Use `AsyncMock` for awaited methods; assert with `assert_awaited_*` where relevant.
- Patch symbols at their import site: `cogs.<module>.<symbol>`.
- Keep external database and network calls mocked; tests must run offline.
- Exercise callback functions directly and assert sends, saves, deletes, and state changes.
- Prefer small fixtures/helpers local to the test module; make async setup explicit.
- Share helpers only through underscore fixture modules. Never import from another `test_*.py` module or construct
  another TestCase as a fixture; fixtures register patches and cleanups on the calling test.
- Invoke registered callbacks through their current binding (`callback.func(cog, ctx, ...)`).
- Bound every wait on an event, future or task (`asyncio.timeout(5)` / `anyio.fail_after(5)`); never use fixed
  sleeps. Fake clocks for throttles and polling delays.
- Pin the raid-weekend clock when behavior depends on it; the suite must pass whether or not a raid weekend is
  in progress.
- Merge input-only variants into one `subTest` table instead of near-duplicate tests.
- Run the canonical unittest command after test or cog changes.

## Anti-patterns

- Do not contact a real database, Discord API, filesystem download, or network endpoint.
- Do not patch the defining module when the cog imported the symbol elsewhere.
- Do not use synchronous mocks for awaited behavior or assert only that code did not crash.
- Do not depend on test order, wall-clock timing, global state, or credentials from `.env`.
- Do not broaden fixtures to hide missing assertions; verify the observable interaction and result.
- Do not pin prose, source text (`inspect.getsource`), package layout, `__all__` order, or library/Pydantic defaults.
- Do not write into the repository from tests; use `tempfile` directories (`tests/_artifacts/` is ignored).

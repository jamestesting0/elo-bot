# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-01
**Commit:** 7ca4daa
**Branch:** main

## OVERVIEW

Discord bot for Clan Capital Elo matchmaking, built on `interactions.py`, `coc.py`, MongoDB, and Beanie.
Production and beta runtimes share the same root-script architecture and dynamically load feature extensions from `cogs/`.

## STRUCTURE

```text
EloBot/
├── main.py / beta.py       # Mirrored production and beta bootstraps
├── watchdog.py             # Supervises and restarts main.py
├── cogs/                   # Auto-loaded Discord feature extensions
├── common/                 # Deployment constants and shared behavior
├── database_models/        # MongoDB/Beanie persistence models
├── tests/                  # Async unittest suites with mocked external boundaries
├── assets/                 # Tracked user content plus ignored building-image corpus
├── data/                   # Ignored mutable JSON runtime state
└── temp/                   # Disposable command/download scratch space
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Runtime startup and global error handling | `main.py`, `beta.py` | Files are intentionally mirrored; keep behavior aligned |
| Process supervision | `watchdog.py` | Runs `main.py`; 30-second checks, 600-second restart threshold |
| Matchmaking, results, base-store state | `cogs/match_commands.py` | Central 5.5k-line orchestration hub |
| Player and Elo presentation | `cogs/player_commands.py`, `cogs/leaderboard_commands.py` | Shared helpers create cross-cog coupling |
| Base lifecycle | `cogs/base_commands.py` | CRUD, pagination, expiration, leaderboard |
| Mongo/Pydantic schema | `database_models/` | Collection names, indexes, singleton and TTL behavior |
| Deployment IDs and tuning | `common/constants.py` | Environment variables plus Discord IDs and Elo constants |
| Shared calculations and I/O | `common/utils.py` | Elo math, JSON, network/image, extension discovery |
| Regression behavior | `tests/test_match_commands.py` | Largest suite; queue, match, result, and slot reconciliation |
| Discord QA, UI and copy conventions | `docs/README.md` | Firefox test accounts, beta channels, embeds, emojis, pagination and concise messages |
| CI contract | `.github/workflows/python-tests.yml` | Python 3.12.10 and unittest discovery |

## CODE MAP

Graph/LSP indexing was unavailable during generation; references below come from source imports and call sites.

| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `main` | async function | `main.py`, `beta.py` | CoC login, Mongo/Beanie setup, bot state, cog loading |
| `get_extensions` | function | `common/utils.py` | Discovers every non-underscore Python file in `cogs/` |
| `Match` / `Player` / `Queue` | Beanie documents | `database_models/` | Core matchmaking persistence |
| `ServerState.current` | class method | `database_models/server_state_models.py` | Singleton operational state |
| `clear_all_base_store_slots` | async function | `common/utils.py` | Reconciles match and clan base-slot state |
| `MatchCommands` | extension | `cogs/match_commands.py` | Match lifecycle, queue, bases, results, scheduled work |
| `PlayerCommands` | extension | `cogs/player_commands.py` | Registration, profiles, history, player statistics |
| `LeaderboardCommands` | extension | `cogs/leaderboard_commands.py` | Metric helpers, cached rankings, pagination |
| `BotWatchdog` | class | `watchdog.py` | Child-process health and restart policy |

## CONVENTIONS

- Python 3.12 runtime; dependencies are pinned directly in `requirements.txt`. There is no package/build metadata.
- `.env` supplies `BOT_MAIN_TOKEN`, `BOT_BETA_TOKEN`, `COC_API_EMAIL`, `COC_API_PASSWORD`, and `MONGO_DB_URL`.
- `VERSION` follows the bootstrap filename; main uses `elo_bot`, beta uses `elo_beta`.
- `main.py` and `beta.py` are byte-identical. Apply bootstrap changes to both and verify they remain synchronized.
- Discord handlers and persistence are async-first. Beanie calls and interaction responses must be awaited.
- Wrap embed titles and field names in bold Markdown (`**...**`), following the existing bot styling.
- Match existing pagination: primary `◀️️` / `▶️` buttons, disabled at page boundaries, and the shared secondary page-number button for views with page jumps. Use Back / Next for form steps.
- Keep Final correction/refund notes in audit storage and private staff confirmation. Player match pages show current official results without staff notes or duplicate correction entries.
- Pylint permits the existing large extension classes and uses a 120-character line limit.
- Mutable JSON state defaults to `data/`; file-writing handlers coordinate through `bot.file_locks`.
- `assets/*.md` and `assets/tips.json` are user-facing runtime inputs, not incidental documentation.

## ANTI-PATTERNS (THIS PROJECT)

- Do not add a non-underscore `.py` file under `cogs/` unless it is safe to import and load as an extension.
- Do not change a Beanie document without checking collection indexes, bootstrap registration, migration impact, and tests.
- Do not commit `.env`, `data/`, `temp/`, or the ignored `assets/clan_capital_buildings/` corpus.
- Do not treat `temp/` as durable storage; clean files in failure paths as well as successful command paths.
- Do not use mutable model defaults such as `records: list = []`; use `Field(default_factory=...)`.
- Do not silently swallow new broad exceptions. Existing Discord delivery suppression is localized behavior, not a default.
- Do not hardcode credentials. Discord deployment IDs already centralized in `common/constants.py` should stay centralized.

## USER-VISIBLE STATE MUST MATCH PERSISTED STATE

- Treat every success response, embed, and enabled button as a claim about committed state. A **Bases Stored** success embed without its corresponding durable slot reservation is a bug.
- Await and verify required database writes and Helper outcomes before announcing success. A submitted request, queued job, in-memory assignment, or database call with no matching record is not proof of completion.
- While an operation is unfinished, show a pending state. On failure, leave a visible failure or incomplete response that says what actually completed and what remains unresolved. Do not erase the only explanation or leave success controls for an operation that failed.
- Distinguish operation failure from Discord delivery failure. If storage committed but sending, editing, or pinning the message failed, retain the reservation and report/recover the publication failure. Do not undo durable success just to make an exception path simpler.
- A failed new action must preserve an earlier successful reservation and its valid message. Roll back only state owned by the failed attempt; never clear unrelated or newer state.
- Persist the identifiers needed to associate a reservation, operation, and message with the exact match, participant, clan, and slot. An old embed is evidence for investigation, not enough information to reconstruct a missing slot. Never guess missing reservation data.

## STORED-BASE RESERVATION LIFECYCLE

Release an established stored-base slot reservation only for these reasons:

1. **Successful Undo:** the exact reservation is undone, with Helper removal confirmed where required.
2. **Completed match:** both participants' complete results and all required record, player, base, and Helper bookkeeping are durably stored.
3. **Raid weekend end:** release reservations covered by the expired weekend, preserving any newer storage.
4. **Confirmed match-channel deletion:** release all reservations belonging to the deleted channel, even if pending operation records must remain for recovery.

- Starting a match does not release stored-base slots. A failed or cancelled Start Match attempt must retain them. Abort, autowin alone, one participant's results, and base submission alone are not release conditions either.
- Use the shared reservation lock and release helpers. Recheck current state under the lock and condition writes on the exact reservation/operation identity so stale callbacks, retries, and cleanup cannot overwrite newer state.
- Confirm channel deletion from its deletion event or an authoritative Discord NotFound response. Cache absence, a missing cached parent, Forbidden, or a transient HTTP error does not establish deletion. A failed deletion attempt must retain reservations.
- After a release, reconcile clan slot availability and retire any surviving stored-base success message and controls. Deleted channels need no message edit. If retirement fails, record the failure for recovery rather than silently accepting a misleading message.
- Keep pending-operation recovery separate from reservation occupancy: retaining an operation record after confirmed channel deletion must not keep that channel's slots occupied.

## VERIFICATION AND CHANGE DISCIPLINE

- For stateful bugs, trace every writer and cleanup path, including callbacks, scheduled jobs, retries, cancellation, restart recovery, and channel deletion. Fix the shared cause and check equivalent flows.
- Regression checks must assert both durable state and the exact user-visible outcome, including ordering. Exercise failed persistence, failed Discord publication, failed Helper work, duplicate/stale actions, and the relevant concurrent cleanup case; checking only that a response was sent is insufficient.
- Use focused tests and local callback/HTTP probes with external services mocked. When invoking an interactions.py registered callback on a test cog, invoke its current binding (`callback.func(cog, ctx)`) where needed; do not accidentally test an earlier cog instance.
- Compare full-suite failures against a fresh pre-change baseline by test identity. Report existing failures separately; do not weaken tests, hide failures, or claim the suite passed when it did not.
- Keep `common/` organized by responsibility: `base_store/`, `discord/`, `matches/`, `results/`, and `players/`. Keep only genuinely general constants and utilities at its root. On module moves, update all active consumers and test patch paths, avoid compatibility shims, and check fresh imports for cycles.
- Preserve unrelated dirty and untracked work. Keep fixes bounded, reuse applicable verification evidence, and stop once the requested behavior is implemented and verified rather than restarting broad review rounds.
- Basedpyright was explicitly rejected for this workspace during this session. Leave its installation alone and do not run it, add its configuration, or chase its diagnostics unless the user requests it again. Use the project's tests and appropriate runtime/syntax checks.

## COMMANDS

```bash
python -m pip install -r requirements.txt
python -m unittest discover -v tests
python main.py
python beta.py
python watchdog.py
```

Maintenance scripts require real service credentials and live data:

```bash
python migrate_helpers.py
python download_bases.py
```

## Helper API
For how to use helper API, the doc can be found in the following link:
https://github.com/tanishq-aggarwal/helper-bot/blob/main/docs/partner-api.md
The complete implementation can be found in:
https://github.com/tanishq-aggarwal/helper-bot

## NOTES

- CI runs only unittest discovery; no formatter, type checker, coverage tool, Docker build, or package build is configured.
- Tests mock Discord, Mongo/Beanie, and network boundaries. The runtime entrypoints and many smaller cogs lack direct coverage.
- Main-only startup schedules database backups. Beta database synchronization is disabled by `BETA_SYNC_FROM_MAIN = False`.
- `data/` is ignored but operationally required by JSON-backed features and migration utilities.
- The working tree may contain untracked operational scripts or generated binary assets; preserve them unless explicitly in scope.

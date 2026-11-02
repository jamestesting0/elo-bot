# `common/` package

## Overview

- Shared runtime contract for both Discord bot entrypoints (`main.py`, `beta.py`).
- Contains process-wide constants and utilities plus packages for Discord, stored bases, matches, results, and players; persistence models live in the sibling `database_models/` package.
- Changes here have high fan-in: cogs, tests, and both deployments import these modules directly.

## Where to look

| Area | File | Contract |
| --- | --- | --- |
| Configuration | `constants.py` | Environment reads, deployment names, IDs, emojis, links, Elo/league tables |
| Persistence | `../database_models/` | Beanie `Document`/Pydantic models, collection names, indexes, state helpers |
| Shared behavior | `utils.py` | Pure parsing/Elo/formatting helpers plus async DB, HTTP, image, and file I/O |
| Discord utilities | `discord/` | Permission checks, decorators, concurrency buckets, channel edits, pagination |
| Stored-base messages | `base_store/` | Legacy message lookup and reservation message retirement; command workflows remain in `cogs/base_store/` |
| Match lifecycle | `matches/` | Cleanup guards, control validation, committed message publication |
| Result processing | `results/` | Validation, result operations, manual results, autowins, record updates, Helper credits |
| Player data coordination | `players/data_boundary.py` | Shared write/reset boundary used by match, data, and season commands |
| Registration | `main.py`, `beta.py` | `init_beanie` document list, override-code indexes, bot lock setup |

## Project-specific conventions

- Import helpers from their owning modules; keep package `__init__.py` files free of eager re-exports and side effects.
- Keep `TOKENS`, `MONGO_DB_URL`, and other secrets sourced from `os.getenv`; do not inline credentials.
- Preserve `DATA_BASES[VERSION]` (`elo_bot`/`elo_beta`) and the matching `TOKENS`/`COC_API_KEYS` deployment keys.
- Treat model field names, collection `Settings.name`, and index definitions as persisted API. Coordinate migrations before renaming.
- Beanie `Document` classes generally use `use_state_management = True`; preserve it when adding mutable state models.
- Use `Field(default_factory=...)` for mutable list/dict defaults. Keep constrained three-slot fields (`conlist(..., min_length=3, max_length=3)`) shape-compatible.
- `ServerState` is a singleton at `_id == "server"`; use its class helpers for atomic updates and fail loudly if migration state is absent.
- `OverrideCode` expiry/uniqueness is enforced by `OVERRIDE_CODE_INDEXES`; register the same model/indexes in both entrypoints.
- Utility functions mix synchronous pure transforms with async Mongo/HTTP/filesystem work; preserve `async` boundaries and await I/O.
- Decorated methods resolve locks from `self.file_locks` or `self.bot.file_locks`; `lock_files` sorts paths before acquiring and releases in `finally`.
- Initialize `bot.file_locks = defaultdict(asyncio.Lock)` and `bot.coc_client_lock` in both `main.py` and `beta.py` before decorated commands run.

## Anti-patterns

- Do not add import-time network/database calls or require optional environment variables during module import.
- Do not bypass Beanie models with ad-hoc collection names, unindexed query fields, or unsynchronized document shapes.
- Do not mutate shared constants (role/ID lists, tier maps, emoji option lists) in command handlers.
- Do not acquire multiple file locks in caller-defined order; use `@lock_files(...)` so ordering stays deterministic.
- Do not hold a file or CoC client lock across unrelated awaits, HTTP downloads, or user interaction.
- Do not convert async utilities to blocking I/O, or call async model helpers without `await`.
- Do not register a document in only one deployment entrypoint; `main.py` and `beta.py` must stay in lockstep.

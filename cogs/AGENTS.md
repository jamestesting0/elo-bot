# COGS KNOWLEDGE BASE

## OVERVIEW

Discord interaction extensions for EloBot. The directory currently contains 17 `*_commands.py` modules; `main.py` and `beta.py` load them dynamically at startup.

## STRUCTURE

- One command domain per `<area>_commands.py`; classes usually subclass `ipy.Extension`.
- `match_commands.py` is the lifecycle hub (5,521 lines), not a small leaf cog.
- `__pycache__/` is generated output; do not edit or document it as source.

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| Matchmaking, match threads, base stores | `cogs/match_commands.py` | Largest stateful workflow; inspect helpers before adding a new path. |
| Player registration and match history | `cogs/player_commands.py` | Owns `fetch_user_match_id_choices`; imports leaderboard helpers. |
| Elo/stat aggregation and displays | `cogs/leaderboard_commands.py` | Shared pure-ish calculations consumed by player and match flows. |
| Challenge flow | `cogs/challenge_commands.py` | Calls `match_commands.is_player_occupied` and `confirm_match`. |
| Thread cleanup | `cogs/thread_commands.py` | Calls match cleanup (`clear_match_base_stores`, `free_clan`, `remove_helper_task`). |
| Startup hooks | `@ipy.listen(ipy.events.Startup)` in cogs | Several cogs rebuild caches, queues, files, or views on bot startup. |
| Persistence model definitions | `database_models/` | Beanie `Document` classes; cogs perform async queries and updates here. |
| Extension discovery and runtime state | `common/utils.py:get_extensions`, `main.py`, `beta.py` | Loader and bot fields are defined outside this directory. |

## PROJECT-SPECIFIC CONVENTIONS

- `get_extensions("cogs")` auto-loads every non-underscore `.py` as `cogs.<module>`; the current 17 files happen to use the `*_commands.py` suffix.
- Keep extension classes importable at module load. Instantiate with `self.bot`; use initialized fields `bot.coc_client`, `bot.file_locks`, `bot.coc_client_lock`, and `bot.match_id_choices`.
- `main.py` and `beta.py` initialize Beanie before loading cogs, then call `bot.load_extension(...)` for each discovered module. Their startup listeners also establish shared runtime behavior.
- Beanie persistence is asynchronous: use `await` for `find_*`, `save_changes`, `update`, and related calls; use `fetch_links=True` only when linked documents are required.
- Cross-cog imports are intentional but directional: `match_commands` → `player_commands`/`leaderboard_commands`; `challenge_commands` and `thread_commands` → `match_commands`; `player_commands` → `leaderboard_commands`.
- Reuse models from `database_models/` and constants, decorators, and utilities from `common/`; command checks and response behavior follow existing `ipy` decorators.

## ANTI-PATTERNS (THIS PROJECT)

- Do not add a non-underscore `.py` helper unless it is safe to auto-load as an extension; put shared helpers in `common/` or an existing cog module.
- Do not import `main.py` or `beta.py` from a cog, or initialize Beanie/bot fields inside a cog; startup owns that ordering.
- Do not make synchronous database calls or mutate Beanie documents without their async save/update operation.
- Do not duplicate match cleanup or occupancy logic across challenge/thread/match flows; call the hub helpers.
- Do not casually split or rewrite `match_commands.py`; map its callers and startup/message listeners first.
- Do not rename shared bot fields or startup listeners without updating both `main.py` and `beta.py`.

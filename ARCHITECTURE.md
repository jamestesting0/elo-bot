# Architecture

EloBot is one Python package, `elobot`, started by two identical launchers (`main.py`, `beta.py`). The launcher's
file name picks the deployment (`main` -> `elo_bot`, `beta` -> `elo_beta`).

## Layers

```text
elobot.app          startup sequence, client, extension registry, root callbacks, error listeners, backups
   |
elobot.extensions   one interactions.py Extension per slash command (decorators, checks, thin handlers)
   |
elobot.features     domain logic per feature: queue, matches, results, base_store, hosting, bases, finals, ...
   |
elobot.db | elobot.discord | elobot.integrations
   |        Beanie models   Discord helpers   Helper API, HTTP, files
elobot.core         pure logic: Elo, parsing, raid weekend, links, finals vocabulary, match results
   |
elobot.config       env settings, Discord IDs, emojis, game tables, presentation constants
```

A layer imports only layers below it. `db`, `discord` and `integrations` are peers and do not import each other.
Extensions never import each other. Both rules are import-linter contracts in `pyproject.toml`, checked in CI
(`lint-imports`). Imports that exist only for type checking are exempt; module-level imports are the rule
(ruff `PLC0415`), except the deferred ML loaders in `elobot/features/bases/fingerprints` and `scripts/`.

## Startup

`main.py` -> `elobot.app.bootstrap.run(__file__)`: derive `VERSION` and build the client
(`elobot.app.client.create_client`, which publishes `elobot.discord.runtime.bot`), then in `main()`: log in to the
CoC API, connect MongoDB, `init_beanie` and create the indexes, start the backup tasks (main only), set the shared
bot fields (`coc_client`, `file_locks`, `coc_client_lock`, `match_id_choices`, `builder_names`,
`base_fingerprints`), load `EXTENSIONS` in order, register the root callbacks (`elobot.app.root_callbacks`) and
start the client. The order is part of the behavior; change it only deliberately.

## Where new code goes

| You are adding | Put it in |
|---|---|
| A slash command, subcommand, button, modal or Task | the matching module in `elobot/extensions` (a mixin for `/match` and `/base`) |
| A new top-level slash command | a new module in `elobot/extensions` plus an entry in `elobot/app/extensions.py:EXTENSIONS` |
| Logic a handler needs that touches the database, Discord or Helper | the feature package in `elobot/features` |
| Embed/component builders and custom IDs | the feature's `presentation.py`/`ui.py` (or the extension, if only it uses them) |
| Named thresholds and tuning values | the feature's `policy.py`, or `elobot/config/game.py` for game-wide tables |
| A Beanie document or embedded model | `elobot/db`, registered with its indexes in `elobot/app/bootstrap.py` |
| A pure calculation with no I/O | `elobot/core` |
| A Discord-wide helper (checks, pagination, edits) | `elobot/discord` |
| A deployment ID, emoji or env setting | `elobot/config` |

A feature never imports an extension: if two extensions need the same code, it belongs in a feature.

## Extensions as mixin packages

`MatchCommands` and `BaseCommands` are composed from mixins (`elobot/extensions/match/*.py`,
`elobot/extensions/base/*.py`). interactions.py registers the callbacks it finds on the class with
`inspect.getmembers`, sorted by attribute name, so the split does not change registration. The slash command and
its groups are defined in the package's `groups.py` and bound as class attributes on the composed class and on each
mixin that decorates with them.

## Changing a handler

Effect order is observable (Discord responses, database writes, Helper calls). When splitting a long handler:
extract contiguous statements only; an extracted block containing `return` returns a result the caller re-checks at
the same point; never move an `await` across another `await`, a lock boundary, a `try`/`finally` or a `ctx`
response; keep exception types and messages; pass locals explicitly.

## Tests

- Patch a name where the code under test looks it up (its import site), not where it is defined.
- When one name is looked up in several modules, `tests/_patch_sites.patch_sites((...), ...)` installs one shared
  mock at each site; `patch_family(MATCH_MODULE_FAMILY | BASE_MODULE_FAMILY, name, ...)` does it for every module of
  the match or base extension package that binds the name.
- `tests/test_contract_snapshots.py` compares the full interaction surface and the persistence contract with
  `tests/snapshots/`. After an intended change run `python -m tests._contract_snapshots --update` and review the diff.
- `COMMANDS.md` maps every command, component, modal, listener and Task to its code; regenerate it with
  `python scripts/commands_index.py` (a unit test fails while it is stale).

## Known follow-ups

- Package-level dependency cycles remain between some feature packages (`base_store` and `hosting`, `matches` and
  `queue`); modules themselves are acyclic. Moving the shared modules would allow an `acyclic_siblings` contract.
- The longest handlers (`challenge_send`, `_start_war`, `_publish_match_results_locked`, `_start_live`,
  `match_autowin`, `match_base_submit`) are candidates for extraction into feature services under the rules above.

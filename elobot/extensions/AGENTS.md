# `elobot.extensions`

One `ipy.Extension` per slash command, loaded in the order of `elobot/app/extensions.py:EXTENSIONS`. Class names
are part of the runtime contract (`bot.get_ext("MatchCommands")`).

- Extensions hold decorators, option parsing, checks, Task definitions and thin handlers. Logic other code needs
  goes to `elobot/features`; extensions never import each other (import-linter enforces it).
- Large extensions are mixin packages: `match/` and `base/`. The composed class keeps the slash command and its
  groups as class attributes (`base = groups.base`) because interactions.py registers commands it finds on the
  class with `inspect.getmembers`. A new mixin goes in the package and in the composed class's bases.
- Mixins bind the command objects they decorate with as class attributes, so decorators referencing a sibling
  command (`@base_info.autocomplete(...)`) must live in the same mixin as that command.
- Handlers use `self.bot`; shared runtime fields (`file_locks`, `coc_client_lock`, `match_id_choices`) are set in
  `elobot/app/bootstrap.py`. Code outside an interaction reads the client from `elobot.discord.runtime`.
- Startup listeners (`@ipy.listen(ipy.events.Startup)`) rebuild caches and views; keep them idempotent.
- Do not split a handler body without the extraction rules in ARCHITECTURE.md; effect order is observable.

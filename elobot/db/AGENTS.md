# `elobot.db`

Beanie documents, embedded Pydantic models and index lists. Field names, `Settings.name` and index definitions are
persisted API: `tests/snapshots/persistence_contract.json` pins them, and changing one needs a migration plan.

- Register every document and its index list in `elobot/app/bootstrap.py` (`init_beanie`); there is one bootstrap for
  main and beta.
- Documents generally use `use_state_management = True`; keep it when adding mutable state models.
- Use `Field(default_factory=...)` for mutable defaults. Keep constrained three-slot fields
  (`conlist(..., min_length=3, max_length=3)`) shape-compatible.
- `ServerState` is a singleton at `_id == "server"`; use its class helpers for atomic updates and fail loudly if
  migration state is absent.
- `OverrideCode` expiry and uniqueness come from `OVERRIDE_CODE_INDEXES`.
- This package imports only `elobot.core` and `elobot.config`. Types embedded in documents live here
  (`fingerprint_types.py`), not in a feature package.
- No import-time network or database calls; never bypass a model with an ad-hoc collection name or unindexed query.

# Stored-base reservations

Stored bases are Clan Capital layouts a participant pre-stores in a resource clan slot before a match starts.
This package owns the reservation records on `Match.participants[i].base_store`, the slot accounting per clan,
the success/control messages, and raid-weekend auto-storage.

| Concern | Module |
|---|---|
| Reserve a slot (shared lock `BASE_STORE_RESERVATION_LOCK`) | `preparation.py` |
| Store, clear, sync and release slots | `storage.py`, `slots.py`, `repository.py` |
| Success embeds, controls, retirement | `presentation.py`, `publication.py`, `messages.py`, `controls.py` |
| Live-match stored bases | `live*.py` |
| `/self` stored bases | `self_service.py` |
| Raid weekend auto-store | `raid_policy.py`, `raid_task.py` |
| Start of a match on stored bases | `start.py` |
| Wrappers the match extension calls (test patch seam) | `match_adapters.py` |

## Reservation lifecycle

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

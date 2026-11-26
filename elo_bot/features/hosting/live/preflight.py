from dataclasses import dataclass

import interactions as ipy


@dataclass(frozen=True, slots=True)
class LivePreflight:
    clan: str | None
    automatic: bool
    host: ipy.BaseUser | None = None
    requested_by: str | None = None
    defer_response: bool = True
    public_progress: bool = False
    district: str | None = None
    allow_opponent_cancel: bool = False
    stored_reservation_id: str | None = None
    sequential: bool | None = False
    attacks_first: int | None = None

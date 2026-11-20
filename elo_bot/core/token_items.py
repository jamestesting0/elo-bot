from dataclasses import dataclass
from enum import StrEnum


class TokenItem(StrEnum):
    PRIORITY_PRO = "queue_priority_pro"
    PRIORITY_ULTRA = "queue_priority_ultra"
    DISTRICT = "district_choice"
    CHALLENGE = "challenge_override"
    PROTECTION = "rank_protection"


@dataclass(frozen=True, slots=True)
class ItemListing:
    name: str
    price: int
    description: str


ITEMS = {
    TokenItem.PRIORITY_PRO: ItemListing("Queue Priority Pro", 800, "Start one queue tier higher."),
    TokenItem.PRIORITY_ULTRA: ItemListing("Queue Priority Ultra", 1600, "Start two queue tiers higher."),
    TokenItem.DISTRICT: ItemListing("District Choice", 1800, "Reserve one eligible district for a queued match."),
    TokenItem.CHALLENGE: ItemListing("Challenge Override", 2400, "Get a private code for one challenge."),
    TokenItem.PROTECTION: ItemListing("Rank Protection", 3600, "Protect your league boundary on one loss per season."),
}

MAX_TOKENS = 2**63 - 1

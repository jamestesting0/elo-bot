from typing import Literal

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel

from elo_bot.core.token_items import TokenItem


class TokenWallet(Document):
    id: str
    user_id: str
    identity_claims: list[str] = Field(default_factory=list)
    balance: int = 0
    initialized: bool = False
    version: int = 0
    inventory: dict[TokenItem, int] = Field(default_factory=dict)

    class Settings:
        name = "token_wallets"
        use_state_management = True
        indexes = [
            IndexModel("user_id", unique=True, name="token_wallet_owner"),
            IndexModel("identity_claims", unique=True, name="token_wallet_identity"),
            IndexModel([("balance", -1), ("user_id", 1)], name="token_wallet_ranking"),
        ]


class TokenLedgerEntry(Document):
    id: str
    operation_key: str
    wallet_id: str
    actor_id: str
    amount: int
    escrow_amount: int = 0
    kind: Literal["grant", "stake", "edit", "cancel", "payout", "refund", "reversal", "identity", "adjust", "switch",
                  "purchase", "match_hold", "match_payout", "match_refund", "match_correction",
                  "activity_reward", "prediction_reward", "item_adjust"]
    identity_from: str | None = None
    identity_to: str | None = None
    market_id: str | None = None
    match_id: str | None = None
    item: TokenItem | None = None
    item_quantity: int = 0
    generation: int | None = None
    created_at: int

    class Settings:
        name = "token_ledger"
        use_state_management = True
        indexes = [
            IndexModel("operation_key", unique=True, name="token_receipt_operation"),
            IndexModel([("wallet_id", 1), ("created_at", -1)], name="token_receipt_history"),
            IndexModel("market_id", name="token_receipt_market"),
        ]


class TokenItemUse(Document):
    id: str
    wallet_id: str
    item: TokenItem
    slot: Literal["queue", "district", "challenge", "protection", "exp"]
    active: bool = True
    target: str | None = None
    state: Literal["queued", "armed", "reserved", "used", "returned"] = "armed"
    activation_order: int = 0
    batch_size: int = 1
    district: str | None = None
    expires_at: int | None = None
    season: int | None = None
    protection_elo: int | None = None

    class Settings:
        name = "token_item_uses"
        indexes = [
            IndexModel([("wallet_id", 1), ("slot", 1)], unique=True,
                       partialFilterExpression={"active": True, "state": {"$in": ["armed", "reserved"]}},
                       name="token_item_head"),
            IndexModel([("wallet_id", 1), ("slot", 1), ("state", 1), ("activation_order", 1), ("_id", 1)],
                       name="token_item_activation_order"),
            IndexModel([("target", 1), ("state", 1)], name="token_item_target"),
            IndexModel([("wallet_id", 1), ("slot", 1), ("season", 1)], name="token_item_usage"),
            IndexModel([("slot", 1), ("state", 1), ("expires_at", 1)], name="token_item_expiry"),
        ]


class ParticipantStake(Document):
    id: str
    final_id: str | None = None
    users: tuple[str, str]
    wallets: tuple[str, str] | None = None
    author: str
    amount: int
    contributions: dict[str, int] = Field(default_factory=dict)
    all_in_users: list[str] = Field(default_factory=list)
    state: Literal["offer", "locked", "closed", "settled"] = "offer"
    revision: int = 1
    settlement_key: str | None = None
    paid: dict[str, int] = Field(default_factory=dict)
    publication_pending: bool = True

    def committed_amount(self, user: str) -> int:
        return self.contributions.get(user, self.amount)

    class Settings:
        name = "participant_stakes"
        indexes = [
            IndexModel([("final_id", 1), ("state", 1)], name="participant_stake_final_state"),
            IndexModel([("wallets", 1), ("state", 1)], name="participant_stake_wallet_state"),
            IndexModel("state", name="participant_stake_state"),
            IndexModel("publication_pending", name="participant_stake_publication"),
        ]


class TokenAward(Document):
    id: str
    source: str
    user_id: str
    kind: Literal["activity_reward", "prediction_reward"]
    amount: int
    revision: str
    wallet_id: str | None = None
    paid: int = 0
    pending: bool = True

    class Settings:
        name = "token_awards"
        indexes = [
            IndexModel([("source", 1), ("kind", 1)], name="token_award_source"),
            IndexModel([("pending", 1), ("kind", 1)], name="token_award_pending"),
        ]


class FrozenStake(BaseModel):
    bet_id: str
    contestant: str
    amount: int


class SettlementGeneration(BaseModel):
    version: int
    result_version: int
    winner: str | None = None
    refund: bool = False
    allocations: dict[str, int] = Field(default_factory=dict)
    paid: list[str] = Field(default_factory=list)
    reversed: list[str] = Field(default_factory=list)


class FinalBettingMarket(Document):
    id: str
    final_id: str
    node_id: str
    contestant_revision: int
    contestants: list[str]
    state: Literal["OPEN", "CLOSED", "SETTLING", "PAID", "REFUNDED", "CORRECTING"] = "OPEN"
    version: int = 0
    pools: dict[str, int] = Field(default_factory=dict)
    escrow: int = Field(default=0, ge=0)
    first_actual_started_at: int | None = None
    start_claim: str | None = None
    stakes: dict[str, FrozenStake] = Field(default_factory=dict)
    generations: list[SettlementGeneration] = Field(default_factory=list)
    desired_result_version: int | None = None
    desired_winner: str | None = None
    desired_refund: bool = False
    settled_result_version: int | None = None
    void_reason: str | None = None
    void_actor: str | None = None
    announcement_attempted: bool = False
    announcement_id: str | None = None
    announcement_channel_id: int | None = None
    announcement_signature: str | None = None
    publication_error: str | None = None

    class Settings:
        name = "final_betting_markets"
        use_state_management = True
        indexes = [IndexModel([("final_id", 1), ("node_id", 1), ("contestant_revision", 1)],
                              unique=True, name="final_market_contestants")]


class FinalBet(Document):
    id: str
    market_id: str
    wallet_id: str
    user_id: str
    contestant: str
    amount: int = Field(ge=0)
    active: bool = True
    version: int = 1
    action_id: str

    class Settings:
        name = "final_bets"
        use_state_management = True
        indexes = [IndexModel([("market_id", 1), ("wallet_id", 1)], unique=True, name="final_bet_wallet")]

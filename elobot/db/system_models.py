import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel, ReturnDocument


class HelperHistory(BaseModel):
    time: int | float
    player1: str
    player2: Optional[str] = None


class Helper(Document):
    user_id: str
    points: float = 0
    history: List[HelperHistory] = Field(default_factory=list)
    accounts: List[str] = Field(default_factory=list)
    operation_receipts: list[str] = Field(default_factory=list)

    class Settings:
        name = "helpers"
        indexes = [
            "user_id"
        ]
        use_state_management = True


class DiscordMessageRef(BaseModel):
    channel: int
    message: int


SERVER_STATE_ID = "server"
OVERRIDE_CODE_LIFETIME = timedelta(days=1)
OverrideType = Literal["challenge", "match"]


class ServerState(Document):
    id: str = Field(default=SERVER_STATE_ID, alias="_id")
    last_matching: float = 0
    queue_disabled: bool = False
    last_tip_index: int = -1
    clan_availability_embed: Optional[DiscordMessageRef] = None
    hosting_queue_embed: Optional[DiscordMessageRef] = None

    @classmethod
    async def current(cls) -> "ServerState":
        state = await cls.get(SERVER_STATE_ID)
        if state is None:
            raise RuntimeError("Migrated server state is missing")  # noqa: GENERIC_ERR_OK
        return state

    @classmethod
    async def update_fields(cls, **values: object) -> None:  # noqa: OBJECT_OK
        result = await cls.get_pymongo_collection().update_one(
            {"_id": SERVER_STATE_ID},
            {"$set": values},
        )
        if result.matched_count == 0:
            raise RuntimeError("Migrated server state is missing")  # noqa: GENERIC_ERR_OK

    @classmethod
    async def advance_tip_index(cls, tip_count: int) -> int:
        if tip_count <= 0:
            raise ValueError("tip_count must be positive")  # noqa: GENERIC_ERR_OK

        state = await cls.get_pymongo_collection().find_one_and_update(
            {"_id": SERVER_STATE_ID},
            [{
                "$set": {
                    "last_tip_index": {
                        "$mod": [
                            {"$add": [{"$ifNull": ["$last_tip_index", -1]}, 1]},
                            tip_count,
                        ]
                    }
                }
            }],
            return_document=ReturnDocument.AFTER,
        )
        if state is None:
            raise RuntimeError("Migrated server state is missing")  # noqa: GENERIC_ERR_OK
        return int(state["last_tip_index"])

    class Settings:
        name = "server_state"


OVERRIDE_CODE_INDEXES = [
    IndexModel([("code", ASCENDING)], unique=True, name="override_code_unique"),
    IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="override_code_expiry"),
]


class OverrideCode(Document):
    code: str
    override_type: OverrideType
    expires_at: datetime

    @classmethod
    async def issue(cls, override_type: OverrideType) -> str:
        code = secrets.token_hex(8)
        await cls.get_pymongo_collection().insert_one({
            "code": code,
            "override_type": override_type,
            "expires_at": datetime.now(timezone.utc) + OVERRIDE_CODE_LIFETIME,
        })
        return code

    @classmethod
    async def is_valid(
            cls,
            override_type: OverrideType,
            code: str | None,
    ) -> bool:
        if not code:
            return False
        result = await cls.get_pymongo_collection().find_one(
            {
                "code": code,
                "override_type": override_type,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"_id": 1},
        )
        return result is not None

    @classmethod
    async def consume(
            cls,
            override_type: OverrideType,
            code: str | None,
    ) -> bool:
        if not code:
            return False
        result = await cls.get_pymongo_collection().find_one_and_delete(
            {
                "code": code,
                "override_type": override_type,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            }
        )
        return result is not None

    class Settings:
        name = "override_codes"
        indexes = OVERRIDE_CODE_INDEXES


__all__ = [
    "HelperHistory", "Helper", "DiscordMessageRef", "SERVER_STATE_ID", "OVERRIDE_CODE_LIFETIME",
    "OverrideType", "ServerState", "OVERRIDE_CODE_INDEXES", "OverrideCode",
]

# noqa: SIZE_OK — job models, transport clients, and status formatting form one API boundary

from collections.abc import Awaitable, Callable
import logging
from typing import Literal, assert_never

import aiohttp
import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


HELPER_POLL_INTERVAL_SECONDS = 2
HELPER_MAX_STATUS_FAILURES = 3
type HelperJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
type HelperJobType = Literal[
    "host_war_match",
    "host_live_match",
    "host_live_match_on_stored",
    "kick_and_reinvite",
    "invite_spectator",
    "fetch_score",
    "fetch_live_score",
    "store_bases",
    "host_war_match_on_stored",
    "remove_bases",
    "preserve_base_link",
    "get_base_image",
]
type HelperScoreAttackLimit = Literal[2, -2]
type HelperStoredBaseSlot = Literal[1, 2]


class HelperModel(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)


class HelperContractMismatchError(ValueError):
    pass


class HelperJobCreated(HelperModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")


class HelperJobCreateAcceptedInvalid(HelperModel):
    pass


class HelperJobCreateRejected(HelperModel):
    error: str | None


class HelperJobCreateUnavailable(HelperModel):
    pass


class HelperEta(HelperModel):
    estimate_start_at: float | None = Field(allow_inf_nan=False)
    estimate_end_at: float | None = Field(allow_inf_nan=False)


class HelperEtaFailure(HelperModel):
    reason: str


class HelperJobError(HelperModel):
    error: str


class HelperJobCreateConflict(HelperJobCreateUnavailable):
    error: str
    existing_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    existing_job_status: Literal["queued", "running"]


class HelperJobEvent(HelperModel):
    t: float
    message: str


class HelperAttack(HelperModel):
    attack_number: int | None = Field(ge=1)
    stars: int | None = Field(ge=0, le=3)
    destruction: float | None = Field(ge=0, le=100)
    miner_count: int | None = Field(ge=-1, le=10)
    invalid_replay: bool


class HelperScore(HelperModel):
    attacker_name_verified: bool
    no_of_valid_attacks: int = Field(ge=0)
    total_no_of_attacks: int | None = Field(ge=0)
    same_attacker_for_all_attacks: bool
    stars_gained: int | None = Field(ge=0, le=3)
    destruction_gained: float | None = Field(ge=0, le=100)
    total_destruction_on_challenge: float | None = Field(ge=0, le=100)
    attacks: list[HelperAttack]


class HelperScoreResult(HelperModel):
    scores: dict[str, HelperScore | None]


class HelperLivePlayerScoreResult(HelperScoreResult):
    player_tag: str


class HelperLiveScores(HelperModel):
    player_1: HelperLivePlayerScoreResult
    player_2: HelperLivePlayerScoreResult


class HelperLiveScoreResult(HelperModel):
    scores: HelperLiveScores


class HelperBaseImageResult(HelperModel):
    image: str = Field(pattern=r"^[0-9a-f]{32}\.webp$")


class HelperJob(HelperModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    job_type: HelperJobType = Field(alias="type")
    status: HelperJobStatus
    created_at: float
    started_at: float | None
    finished_at: float | None
    events: list[HelperJobEvent]
    result: HelperScoreResult | HelperLiveScoreResult | HelperBaseImageResult | None
    error: str | None

    @model_validator(mode="after")
    def validate_result_type(self) -> "HelperJob":
        match self.job_type:
            case (
                "host_war_match"
                | "host_live_match"
                | "host_live_match_on_stored"
                | "kick_and_reinvite"
                | "invite_spectator"
                | "store_bases"
                | "host_war_match_on_stored"
                | "remove_bases"
                | "preserve_base_link"
            ):
                if self.result is None:
                    return self
            case "fetch_score":
                if self.result is None or isinstance(self.result, HelperScoreResult):
                    return self
            case "fetch_live_score":
                if self.result is None or isinstance(self.result, HelperLiveScoreResult):
                    return self
            case "get_base_image":
                if isinstance(self.result, HelperBaseImageResult) or (
                        self.result is None and self.status != "succeeded"):
                    return self
            case unexpected:
                assert_never(unexpected)
        raise HelperContractMismatchError(f"{self.job_type} returned an incompatible result.")


class HelperHostWarMatchPayload(HelperModel):
    host_clan_tag: str
    player_tag: str
    base_links: list[str] = Field(min_length=1, max_length=9)
    slot: HelperStoredBaseSlot
    opponent_player_tag: str | None = None
    kick_opponent: bool | None = None


class HelperHostLiveMatchPayload(HelperModel):
    host_clan_tag: str
    player_1_tag: str
    player_2_tag: str
    base_links_for_player_1: list[str] = Field(min_length=1, max_length=9)
    base_links_for_player_2: list[str] = Field(min_length=1, max_length=9)


class HelperHostLiveMatchOnStoredPayload(HelperModel):
    host_clan_tag: str
    player_1_tag: str
    player_2_tag: str
    districts: list[str]
    remove_bases_after: Literal[False]


class HelperKickAndReinvitePayload(HelperModel):
    host_clan_tag: str
    player_tag: str


class HelperInviteSpectatorPayload(HelperModel):
    host_clan_tag: str
    player_tag: str


class HelperFetchScorePayload(HelperModel):
    host_clan_tag: str
    attacker_tag: str
    attacks_started_after: float
    attacks_ended_before: float
    districts: list[str]
    max_allowed_attacks: dict[str, int]


class HelperFetchLiveScorePayload(HelperModel):
    host_clan_tag: str
    player_1_tag: str
    player_2_tag: str
    attacks_started_after: float
    attacks_ended_before: float
    districts: list[str]
    max_allowed_attacks: dict[str, int]


class HelperStoreBasesPayload(HelperModel):
    host_clan_tag: str
    base_links: list[str] = Field(min_length=1, max_length=9)
    slot: HelperStoredBaseSlot


class HelperHostWarMatchOnStoredPayload(HelperModel):
    host_clan_tag: str
    player_tag: str
    districts: list[str]
    slot: HelperStoredBaseSlot
    remove_bases_after: Literal[False]


class HelperRemoveBasesPayload(HelperModel):
    host_clan_tag: str
    districts: list[str]
    slot: HelperStoredBaseSlot


class HelperPreserveBaseLinkPayload(HelperModel):
    base_link: str


class HelperGetBaseImagePayload(HelperModel):
    base_link: str
    scout_view: Literal[False] = False
    filetype: Literal["webp"] = "webp"
    keep_secret: bool


class HelperJobCreateRequest(HelperModel):
    job_type: HelperJobType = Field(alias="type")
    requested_by: str
    payload: (
        HelperHostWarMatchPayload
        | HelperHostLiveMatchPayload
        | HelperHostLiveMatchOnStoredPayload
        | HelperKickAndReinvitePayload
        | HelperInviteSpectatorPayload
        | HelperFetchScorePayload
        | HelperFetchLiveScorePayload
        | HelperStoreBasesPayload
        | HelperHostWarMatchOnStoredPayload
        | HelperRemoveBasesPayload
        | HelperPreserveBaseLinkPayload
        | HelperGetBaseImagePayload
    )

    @model_validator(mode="after")
    def validate_payload_type(self) -> "HelperJobCreateRequest":
        match self.job_type:
            case "host_war_match":
                if isinstance(self.payload, HelperHostWarMatchPayload):
                    return self
            case "host_live_match":
                if isinstance(self.payload, HelperHostLiveMatchPayload):
                    return self
            case "host_live_match_on_stored":
                if isinstance(self.payload, HelperHostLiveMatchOnStoredPayload):
                    return self
            case "kick_and_reinvite":
                if isinstance(self.payload, HelperKickAndReinvitePayload):
                    return self
            case "invite_spectator":
                if isinstance(self.payload, HelperInviteSpectatorPayload):
                    return self
            case "fetch_score":
                if isinstance(self.payload, HelperFetchScorePayload):
                    return self
            case "fetch_live_score":
                if isinstance(self.payload, HelperFetchLiveScorePayload):
                    return self
            case "store_bases":
                if isinstance(self.payload, HelperStoreBasesPayload):
                    return self
            case "host_war_match_on_stored":
                if isinstance(self.payload, HelperHostWarMatchOnStoredPayload):
                    return self
            case "remove_bases":
                if isinstance(self.payload, HelperRemoveBasesPayload):
                    return self
            case "preserve_base_link":
                if isinstance(self.payload, HelperPreserveBaseLinkPayload):
                    return self
            case "get_base_image":
                if isinstance(self.payload, HelperGetBaseImagePayload):
                    return self
            case unexpected:
                assert_never(unexpected)
        raise HelperContractMismatchError(f"{self.job_type} received an incompatible payload.")


type HelperJobCreateOutcome = (
    HelperJobCreated
    | HelperJobCreateAcceptedInvalid
    | HelperJobCreateRejected
    | HelperJobCreateUnavailable
)


class HelperTrackingFailure(HelperModel):
    job_id: str
    reason: str


class HelperEtaReader:
    def __init__(self, api_base_url: str, api_key: str, job_type: HelperJobType) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.job_type = job_type

    async def read(self) -> HelperEta | HelperEtaFailure:
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.api_base_url}/v1/eta",
                    params={"type": self.job_type},
                    headers={"X-Api-Key": self.api_key},
                ) as response:
                    match response.status:  # noqa: MATCH_OK
                        case 200:
                            try:
                                return HelperEta.model_validate_json(await response.text())
                            except ValidationError:
                                return HelperEtaFailure(reason="Helper returned invalid availability data.")
                        case _:
                            return HelperEtaFailure(reason="Helper availability could not be checked.")
        except (aiohttp.ClientError, TimeoutError):
            return HelperEtaFailure(reason="Helper availability could not be checked.")


class HelperJobCreator:
    def __init__(self, api_base_url: str, api_key: str) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key

    async def create(
            self,
            session: aiohttp.ClientSession,
            request: HelperJobCreateRequest,
    ) -> HelperJobCreateOutcome:
        try:
            async with session.post(
                f"{self.api_base_url}/v1/jobs",
                headers={"X-Api-Key": self.api_key},
                json=request.model_dump(by_alias=True, exclude_none=True),
            ) as response:
                match response.status:  # noqa: MATCH_OK
                    case 201:
                        try:
                            return HelperJobCreated.model_validate_json(await response.text())
                        except ValidationError:
                            return HelperJobCreateAcceptedInvalid()
                    case 400:
                        try:
                            error = HelperJobError.model_validate_json(await response.text())
                            return HelperJobCreateRejected(error=error.error)
                        except ValidationError:
                            return HelperJobCreateRejected(error=None)
                    case 409:
                        try:
                            return HelperJobCreateConflict.model_validate_json(await response.text())
                        except ValidationError:
                            return HelperJobCreateUnavailable()
                    case _:
                        return HelperJobCreateUnavailable()
        except (aiohttp.ClientError, TimeoutError):
            return HelperJobCreateUnavailable()


class HelperJobCanceller:
    def __init__(self, api_base_url: str, api_key: str) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key

    async def cancel(self, session: aiohttp.ClientSession, job_id: str) -> None:
        async with session.post(
            f"{self.api_base_url}/v1/jobs/{job_id}/cancel",
            headers={"X-Api-Key": self.api_key},
        ) as response:
            response.raise_for_status()


class HelperJobPoller:
    def __init__(self, api_base_url: str, api_key: str) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key

    async def read(
            self, session: aiohttp.ClientSession, job_id: str) -> HelperJob | HelperTrackingFailure:
        try:
            async with session.get(
                f"{self.api_base_url}/v1/jobs/{job_id}",
                headers={"X-Api-Key": self.api_key},
            ) as response:
                match response.status:  # noqa: MATCH_OK
                    case 200:
                        try:
                            return HelperJob.model_validate_json(await response.text())
                        except ValidationError:
                            return HelperTrackingFailure(
                                job_id=job_id,
                                reason="the status API returned invalid job data.",
                            )
                    case 401:
                        return HelperTrackingFailure(
                            job_id=job_id,
                            reason="the status API rejected the bot credentials.",
                        )
                    case 404:
                        return HelperTrackingFailure(
                            job_id=job_id,
                            reason="the job was not found or does not belong to this bot.",
                        )
                    case status if status >= 500:
                        return HelperTrackingFailure(
                            job_id=job_id,
                            reason="the status API is currently unavailable.",
                        )
                    case _:
                        return HelperTrackingFailure(
                            job_id=job_id,
                            reason="the status API rejected the request.",
                        )
        except (aiohttp.ClientError, TimeoutError):
            return HelperTrackingFailure(
                job_id=job_id,
                reason="the status API could not be reached.",
            )

    async def wait(
            self,
            session: aiohttp.ClientSession,
            job_id: str,
            update_progress: Callable[[HelperJob], Awaitable[None]],
    ) -> HelperJob | HelperTrackingFailure:
        failures = 0
        while True:
            try:
                async with session.get(
                    f"{self.api_base_url}/v1/jobs/{job_id}",
                    headers={"X-Api-Key": self.api_key},
                ) as response:
                    match response.status:  # noqa: MATCH_OK
                        case 200:
                            try:
                                job = HelperJob.model_validate_json(await response.text())
                            except ValidationError:
                                return HelperTrackingFailure(
                                    job_id=job_id,
                                    reason="the status API returned invalid job data.",
                                )
                            failures = 0
                        case 401:
                            return HelperTrackingFailure(
                                job_id=job_id,
                                reason="the status API rejected the bot credentials.",
                            )
                        case 404:
                            return HelperTrackingFailure(
                                job_id=job_id,
                                reason="the job was not found by the status API.",
                            )
                        case status if status >= 500:
                            failures += 1
                            if failures >= HELPER_MAX_STATUS_FAILURES:
                                return HelperTrackingFailure(
                                    job_id=job_id,
                                    reason="the status API was unavailable after three attempts.",
                                )
                            await anyio.sleep(HELPER_POLL_INTERVAL_SECONDS)
                            continue
                        case _:
                            return HelperTrackingFailure(
                                job_id=job_id,
                                reason="the status API rejected the request.",
                            )
            except (aiohttp.ClientError, TimeoutError):
                failures += 1
                if failures >= HELPER_MAX_STATUS_FAILURES:
                    return HelperTrackingFailure(
                        job_id=job_id,
                        reason="the status API could not be reached after three attempts.",
                    )
                await anyio.sleep(HELPER_POLL_INTERVAL_SECONDS)
                continue

            try:
                await update_progress(job)
            except Exception:
                logging.exception("Unable to publish progress for Helper job %s", job.id)
            match job.status:
                case "queued" | "running":
                    await anyio.sleep(HELPER_POLL_INTERVAL_SECONDS)
                case "succeeded" | "failed" | "cancelled":
                    return job
                case unexpected:
                    assert_never(unexpected)


def _fetch_score_summary(result: HelperScoreResult | HelperLiveScoreResult | None, partial: bool) -> str:
    if result is None:
        return "No scores were returned."
    if isinstance(result, HelperLiveScoreResult):
        return " ".join(
            f"{player.player_tag}: {_fetch_score_summary(player, partial)}"
            for player in (result.scores.player_1, result.scores.player_2)
        )
    score_count = sum(score is not None for score in result.scores.values())
    missing_count = len(result.scores) - score_count
    score_label = "score" if score_count == 1 else "scores"
    prefix = "partial " if partial else ""
    summary = f"{score_count} {prefix}{score_label} returned."
    if missing_count:
        district_label = "district" if missing_count == 1 else "districts"
        summary += f" {missing_count} {district_label} without a score."
    return summary


def _generic_job_notification(job: HelperJob) -> str:
    match job.status:
        case "succeeded":
            progress = f" {job.events[-1].message}" if job.events else ""
            return f"Helper job `{job.id}` succeeded.{progress}"
        case "failed":
            detail = job.error or "Helper did not provide an error."
            punctuation = "" if detail.endswith((".", "!", "?")) else "."
            last_event = f"\n\nLast event: {job.events[-1].message}" if job.events else ""
            return f"Helper job `{job.id}` failed: {detail}{punctuation}{last_event}"
        case "cancelled":
            return f"Helper job `{job.id}` was cancelled."
        case "queued" | "running":
            progress = f" {job.events[-1].message}" if job.events else ""
            return f"Helper job `{job.id}` is {job.status}.{progress}"
        case unexpected:
            assert_never(unexpected)


def helper_job_notification(outcome: HelperJob | HelperTrackingFailure) -> str:
    match outcome:
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            return (
                f"Could not monitor Helper job `{job_id}`: {reason} "
                "The war remains active; contact a helper before retrying."
            )
        case HelperJob(id=job_id, job_type="fetch_score" | "fetch_live_score", status="succeeded", result=result):
            match result:
                case None | HelperScoreResult() | HelperLiveScoreResult():
                    return f"Helper score job `{job_id}` succeeded: {_fetch_score_summary(result, False)}"
                case unreachable:
                    assert_never(unreachable)
        case HelperJob(
            id=job_id, job_type="fetch_score" | "fetch_live_score", status="failed",
            result=result, error=error, events=events,
        ):
            detail = error or "Helper did not provide an error."
            punctuation = "" if detail.endswith((".", "!", "?")) else "."
            last_event = f"\n\nLast event: {events[-1].message}" if events else ""
            match result:
                case None | HelperScoreResult() | HelperLiveScoreResult():
                    return (
                        f"Helper score job `{job_id}` failed: {detail}{punctuation} "
                        f"{_fetch_score_summary(result, True)}{last_event}"
                    )
                case unreachable:
                    assert_never(unreachable)
        case HelperJob(id=job_id, job_type="fetch_score" | "fetch_live_score", status="cancelled"):
            return f"Helper score job `{job_id}` was cancelled."
        case HelperJob(id=job_id, job_type="fetch_score" | "fetch_live_score", status="queued" | "running", events=events):
            progress = f" {events[-1].message}" if events else ""
            return f"Helper score job `{job_id}` is {outcome.status}.{progress}"
        case HelperJob(
            job_type=(
                "host_war_match"
                | "host_live_match"
                | "host_live_match_on_stored"
                | "kick_and_reinvite"
                | "invite_spectator"
                | "store_bases"
                | "host_war_match_on_stored"
                | "remove_bases"
                | "preserve_base_link"
                | "get_base_image"
            ),
        ) as job:
            return _generic_job_notification(job)
        case unreachable:
            assert_never(unreachable)

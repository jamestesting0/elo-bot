from typing import Literal, Protocol, assert_never

import aiohttp
import interactions as ipy
from pydantic import Field, ValidationError

from elo_bot.integrations.helper_api import (
    HelperHostWarMatchOnStoredPayload,
    HelperJob,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperRemoveBasesPayload,
    HelperStoreBasesPayload,
    HelperTrackingFailure,
)


type HelperStoredBaseJobType = Literal["store_bases", "host_war_match_on_stored", "remove_bases"]
type HelperStoredBaseTerminalStatus = Literal["succeeded", "failed", "cancelled"]


class HelperStoredBaseRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str


class HelperStoredBaseTerminalJob(HelperJob):
    job_type: HelperStoredBaseJobType = Field(alias="type")
    status: HelperStoredBaseTerminalStatus
    result: None


type HelperStoredBasePollOutcome = HelperStoredBaseTerminalJob | HelperTrackingFailure
type HelperStoredBaseJobOutcome = (
    HelperStoredBasePollOutcome
    | HelperJobCreateRejected
    | HelperJobCreateAcceptedInvalid
    | HelperJobCreateUnavailable
)


def _stored_base_poll_outcome(
        poll_outcome: HelperJob | HelperTrackingFailure,
) -> HelperStoredBasePollOutcome:
    match poll_outcome:
        case HelperTrackingFailure() as tracking_failure:
            return tracking_failure
        case HelperJob(status="succeeded" | "failed" | "cancelled") as terminal_job:
            try:
                return HelperStoredBaseTerminalJob.model_validate(
                    terminal_job.model_dump(by_alias=True),
                )
            except ValidationError:
                return HelperTrackingFailure(
                    job_id=terminal_job.id,
                    reason="the status API returned a terminal job that did not match the stored-base request.",
                )
        case HelperJob(id=job_id):
            return HelperTrackingFailure(
                job_id=job_id,
                reason="the status API returned before the stored-base job became terminal.",
            )
        case unexpected:
            assert_never(unexpected)


async def _run_stored_base_job(
        runtime: HelperStoredBaseRuntime,
        request: HelperJobCreateRequest,
) -> HelperStoredBaseJobOutcome:
    async def discard_job_update(_job: HelperJob) -> None:
        pass

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        create_outcome = await HelperJobCreator(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).create(session, request)
        match create_outcome:
            case HelperJobCreated(id=job_id):
                poll_outcome = await HelperJobPoller(
                    runtime.helper_api_base_url,
                    runtime.helper_api_key,
                ).wait(session, job_id, discard_job_update)
            case (
                HelperJobCreateRejected()
                | HelperJobCreateAcceptedInvalid()
                | HelperJobCreateUnavailable()
            ) as create_failure:
                return create_failure
            case unexpected:
                assert_never(unexpected)

        return _stored_base_poll_outcome(poll_outcome)


async def store_bases(
        runtime: HelperStoredBaseRuntime,
        requested_by: str,
        payload: HelperStoreBasesPayload,
) -> HelperStoredBaseJobOutcome:
    return await _run_stored_base_job(
        runtime,
        HelperJobCreateRequest(
            job_type="store_bases",
            requested_by=requested_by,
            payload=payload,
        ),
    )


async def host_war_match_on_stored(
        runtime: HelperStoredBaseRuntime,
        requested_by: str,
        payload: HelperHostWarMatchOnStoredPayload,
) -> HelperStoredBaseJobOutcome:
    return await _run_stored_base_job(
        runtime,
        HelperJobCreateRequest(
            job_type="host_war_match_on_stored",
            requested_by=requested_by,
            payload=payload,
        ),
    )


async def remove_bases(
        runtime: HelperStoredBaseRuntime,
        requested_by: str,
        payload: HelperRemoveBasesPayload,
) -> HelperStoredBaseJobOutcome:
    return await _run_stored_base_job(
        runtime,
        HelperJobCreateRequest(
            job_type="remove_bases",
            requested_by=requested_by,
            payload=payload,
        ),
    )

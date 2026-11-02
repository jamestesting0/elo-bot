import logging
from pathlib import Path
from typing import assert_never

import aiohttp
import interactions as ipy

from cogs.war_hosting.helper_api import (
    HelperBaseImageResult, HelperGetBaseImagePayload, HelperJob, HelperJobCreateAcceptedInvalid,
    HelperJobCreateConflict, HelperJobCreated, HelperJobCreateRejected, HelperJobCreateRequest,
    HelperJobCreateUnavailable, HelperJobCreator, HelperJobPoller, HelperTrackingFailure, helper_job_notification,
)
from common.base_fingerprints.artifacts import MAX_IMAGE_BYTES
from common.base_fingerprints.extractor import decode_image
from common.base_fingerprints.images import MAX_SCREENSHOT_BYTES
from common.base_fingerprints.types import FingerprintError
from common.constants import ERROR_EMOJI, HELPER_API_BASE_URL, HELPER_API_KEY, LOADING_EMOJI, SUCCESS_EMOJI


async def capture_base_screenshot(
        base_link: str, requested_by: str, filename: str, *, ctx: ipy.SlashContext, keep_secret: bool = False,
) -> bool:
    """Capture and validate a Helper artifact; the caller owns a successful temporary file."""
    path = Path(filename)
    accepted = False
    last_content = f"{LOADING_EMOJI} Requesting a screenshot from Helper..."
    progress = await ctx.send(last_content, ephemeral=True)

    async def report(content: str) -> None:
        nonlocal last_content
        if content == last_content:
            return
        try:
            await ctx.edit(progress, content=content)
        except ipy.errors.HTTPException:
            logging.exception("Unable to update Helper screenshot progress for %s", requested_by)
        else:
            last_content = content

    try:
        if not HELPER_API_BASE_URL or not HELPER_API_KEY:
            await report(f"{ERROR_EMOJI} Helper screenshots are not configured. Contact a helper.")
            return False
        request = HelperJobCreateRequest(
            job_type="get_base_image", requested_by=requested_by,
            payload=HelperGetBaseImagePayload(base_link=base_link, keep_secret=keep_secret),
        )

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            created = await HelperJobCreator(HELPER_API_BASE_URL, HELPER_API_KEY).create(session, request)
            match created:
                case HelperJobCreated(id=job_id):
                    status = "queued"
                case HelperJobCreateConflict(existing_job_id=job_id, existing_job_status=status):
                    pass
                case HelperJobCreateAcceptedInvalid():
                    await report(f"{ERROR_EMOJI} Helper accepted the screenshot request, but its job ID "
                                 "could not be confirmed. Contact a helper before retrying.")
                    return False
                case HelperJobCreateRejected(error=error):
                    detail = f": {error}" if error else "."
                    await report(f"{ERROR_EMOJI} Helper rejected the screenshot request{detail}")
                    return False
                case HelperJobCreateUnavailable():
                    await report(f"{ERROR_EMOJI} Could not confirm that Helper queued the screenshot. "
                                 "Try again later.")
                    return False
                case unexpected:
                    assert_never(unexpected)

            waiting = " Waiting for an available Helper." if status == "queued" else ""
            await report(f"{LOADING_EMOJI} Helper job `{job_id}` is {status}.{waiting}")

            async def update_progress(job: HelperJob) -> None:
                if job.id == job_id and job.job_type == "get_base_image" and job.status in {"queued", "running"}:
                    waiting = " Waiting for an available Helper." if job.status == "queued" and not job.events else ""
                    await report(f"{LOADING_EMOJI} {helper_job_notification(job)}{waiting}")

            terminal = await HelperJobPoller(HELPER_API_BASE_URL, HELPER_API_KEY).wait(
                session, job_id, update_progress,
            )
            match terminal:
                case HelperJob(id=returned_id, job_type="get_base_image", status="succeeded",
                               result=HelperBaseImageResult()) if returned_id == job_id:
                    pass
                case HelperTrackingFailure(job_id=job_id, reason=reason):
                    await report(f"{ERROR_EMOJI} Could not check Helper job `{job_id}`: {reason} "
                                 "The screenshot is not confirmed. Contact a helper before retrying.")
                    return False
                case HelperJob(id=returned_id, job_type="get_base_image", status="failed" | "cancelled") \
                        if returned_id == job_id:
                    await report(f"{ERROR_EMOJI} {helper_job_notification(terminal)}")
                    return False
                case HelperJob():
                    await report(f"{ERROR_EMOJI} Helper returned an unexpected screenshot result for job `{job_id}`.")
                    return False
                case unexpected:
                    assert_never(unexpected)
            await report(f"{LOADING_EMOJI} Helper job `{job_id}` finished. Downloading the screenshot...")
            async with session.get(
                f"{HELPER_API_BASE_URL.rstrip('/')}/v1/jobs/{job_id}/image",
                headers={"X-Api-Key": HELPER_API_KEY},
            ) as response:
                if response.status != 200:
                    await report(f"{ERROR_EMOJI} Helper captured the screenshot, but downloading it failed "
                                 f"(HTTP {response.status}).")
                    return False
                if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                    await report(f"{ERROR_EMOJI} Helper returned a screenshot that is too large.")
                    return False
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > MAX_IMAGE_BYTES:
                        await report(f"{ERROR_EMOJI} Helper returned a screenshot that is too large.")
                        return False
        path.write_bytes(content)
        with decode_image(path) as image:
            if path.stat().st_size > MAX_SCREENSHOT_BYTES:
                for quality in range(90, 19, -10):
                    image.save(path, "WEBP", quality=quality, optimize=True)
                    if path.stat().st_size <= MAX_SCREENSHOT_BYTES:
                        break
        if path.stat().st_size > MAX_SCREENSHOT_BYTES:
            await report(f"{ERROR_EMOJI} The Helper screenshot is too large to upload.")
            return False
        await report(f"{SUCCESS_EMOJI} Helper screenshot ready.")
        accepted = True
        return True
    except (aiohttp.ClientError, TimeoutError):
        await report(f"{ERROR_EMOJI} Helper captured the screenshot, but the image download could not finish.")
        return False
    except FingerprintError:
        await report(f"{ERROR_EMOJI} Helper returned an invalid screenshot.")
        return False
    except OSError:
        await report(f"{ERROR_EMOJI} Could not save the Helper screenshot.")
        return False
    finally:
        if not accepted:
            path.unlink(missing_ok=True)

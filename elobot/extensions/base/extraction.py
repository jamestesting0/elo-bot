import difflib
import logging
import math
import time
from uuid import UUID

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

import elobot.extensions.base.groups as groups
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import (
    LVL1_ROLES,
    WHITELISTED_STAFFS,
)
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout
from elobot.discord.checks import has_any, has_any_id
from elobot.features.bases.fingerprints.removal import (
    FingerprintRemovalProgress,
    remove_all_fingerprints,
    remove_fingerprint,
)
from elobot.features.bases.fingerprints.search import (
    can_search,
)
from elobot.features.bases.fingerprints.types import FingerprintError
from elobot.features.bases.layouts.queries import BaseExtractionAutocompleteRow
from elobot.features.bases.layouts.ui import _builder_choice_label, _fingerprint_status_emoji


class BaseExtractionCommands:
    base = groups.base

    def _extractable_bases(self, ctx):
        if str(ctx.author.id) == str(self.bot.owner.id):
            return BaseLayout.find_all()
        return BaseLayout.find({"$or": [{"quality": {"$ne": "Private"}}, {"builder_id": str(ctx.author.id)}]})

    @base.subcommand(sub_cmd_name="extract", sub_cmd_description="Regenerate one base fingerprint on CPU")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="base_link", description="Select a base or paste its link", opt_type=ipy.OptionType.STRING,
                      autocomplete=True, required=True)
    async def base_extract(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)
        try:
            identity = ({"_id": PydanticObjectId(base_link)} if PydanticObjectId.is_valid(base_link)
                        else {"link": base_link})
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} This base is unavailable or you do not have access to it.",
                               ephemeral=True)
                return
            state, created = await self.bot.base_fingerprints.request_extraction(base)
            status = "queued on CPU" if created else f"already {state.status}"
            await ctx.send(f"{LOADING_EMOJI} Fingerprint extraction {status} for `{base.district}`.\n"
                           f"Check `/base extract_status job_id:{state.request_id}`.",
                           ephemeral=True)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint extraction unavailable: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm the saved extraction request. "
                           "Retry to retrieve its status.",
                           ephemeral=True)

    @base.subcommand(sub_cmd_name="extract_status", sub_cmd_description="Check an extraction job or bulk run")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="job_id", description="Job ID from /base extract or run ID from /base extract_all",
                      opt_type=ipy.OptionType.STRING, required=True)
    async def base_extract_status(self, ctx: ipy.SlashContext, job_id: str):
        await ctx.defer(ephemeral=True)
        try:
            job_id = str(UUID(job_id.strip()))
        except ValueError:
            await ctx.send(f"{ERROR_EMOJI} Invalid job or run ID. "
                           "Paste the ID returned by `/base extract` or `/base extract_all`.",
                           ephemeral=True)
            return
        try:
            run = await BaseExtractionRun.get(job_id)
            if run is not None:
                details = await self.bot.base_fingerprints.status(run)
                if run.notification_error:
                    details += f"\n{ERROR_EMOJI} Channel updates unavailable: `{run.notification_error}`."
                await ctx.send(f"{_fingerprint_status_emoji(run.status)} {details}", ephemeral=True)
                return
            task = await BaseFingerprintTask.get(job_id)
            identity = {"_id": task.base_id} if task else {"fingerprint_extraction.request_id": job_id}
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} Job or run not found, or its base is unavailable to you.",
                               ephemeral=True)
                return
            state = base.fingerprint_extraction
            status, error = (task.state, task.error_code) if task else (state.status, state.error_code)
            if state and state.request_id == job_id and state.completion_receipt:
                status, error = ("succeeded" if state.status == "ready" else state.status), state.error_code
            details = f"Job `{job_id}`: **{status}** ({base.district})."
            if error:
                details += f"\n{ERROR_EMOJI} Error: `{error}`."
            await ctx.send(f"{_fingerprint_status_emoji(status)} {details}", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Job status is temporarily unavailable. Please retry.", ephemeral=True)

    @base.subcommand(sub_cmd_name="unextract", sub_cmd_description="Remove one base fingerprint")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="base_link", description="Select a base or paste its link", opt_type=ipy.OptionType.STRING,
                      autocomplete=True, required=True)
    async def base_unextract(self, ctx: ipy.SlashContext, base_link: str) -> None:
        await ctx.defer(ephemeral=True)
        try:
            identity = ({"_id": PydanticObjectId(base_link)} if PydanticObjectId.is_valid(base_link)
                        else {"link": base_link})
            base = await self._extractable_bases(ctx).find(identity).first_or_none()
            if base is None:
                await ctx.send(f"{ERROR_EMOJI} This base is unavailable or you do not have access to it.", ephemeral=True)
                return
            result = await remove_fingerprint(base, self.bot.base_fingerprints.run_lock)
            status = "Fingerprint removed" if result.removed else "No fingerprint was stored"
            await ctx.send(f"{SUCCESS_EMOJI} {status} for `{base.district}`. Stopped {result.cancelled} pending extractions.",
                           ephemeral=True)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint removal incomplete: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm fingerprint removal. Please retry.", ephemeral=True)

    @base.subcommand(sub_cmd_name="unextract_all", sub_cmd_description="Remove all saved base fingerprints")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def base_unextract_all(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)
        message = await ctx.send(f"{LOADING_EMOJI} **Removing base fingerprints...**", ephemeral=True)
        last_update = -math.inf
        progress_available = True

        async def report_progress(progress: FingerprintRemovalProgress) -> None:
            nonlocal last_update, progress_available
            if not progress_available:
                return
            now = time.monotonic()
            if now - last_update < 1:
                return
            last_update = now
            try:
                await ctx.edit(message, content=f"{LOADING_EMOJI} **Removing base fingerprints...**\n"
                               f"**{progress.processed:,} / {progress.total:,}** bases processed · "
                               f"**{progress.result.removed:,}** fingerprints removed")
            except ipy.errors.HTTPException as error:
                progress_available = False
                logging.warning("Base fingerprint removal progress unavailable: %s", type(error).__name__)

        try:
            result = await remove_all_fingerprints(self.bot.base_fingerprints.run_lock,
                                                   self.bot.base_fingerprints.gpu.close, report_progress)
            changed = f" {result.changed} bases changed during removal; retry to clear them." if result.changed else ""
            emoji = ERROR_EMOJI if result.changed else SUCCESS_EMOJI
            status = (f"{emoji} Removed {result.removed} fingerprints. Stopped {result.cancelled} pending extractions."
                      + changed)
        except FingerprintError as error:
            status = f"{ERROR_EMOJI} Fingerprint removal incomplete: {error}."
        await ctx.edit(message, content=status)

    @base_unextract.autocomplete(option_name="base_link")
    @base_extract.autocomplete(option_name="base_link")
    async def base_extract_autocomplete(self, ctx: ipy.AutocompleteContext):
        if not can_search(ctx, review=True):
            await ctx.send([])
            return
        bases = await self._extractable_bases(ctx).sort("_id").project(BaseExtractionAutocompleteRow).to_list()
        choices = {}
        for base in bases:
            builder = _builder_choice_label(self.bot, ctx.guild, base.builder_id)
            choices[f"{base.district} {builder} {base.link} {base.id}".lower()] = {
                "name": f"{base.district} by {builder} ({str(base.id)[-6:]})"[:100], "value": str(base.id),
            }
        if not ctx.input_text:
            await ctx.send(list(choices.values())[:25])
            return
        user_input = ctx.input_text.lower()
        matches = [key for key in choices if user_input in key][:25]
        if not matches:
            matches = difflib.get_close_matches(user_input, choices, n=25, cutoff=0.15)
        await ctx.send([choices[key] for key in matches])

    @base.subcommand(sub_cmd_name="extract_all",
                     sub_cmd_description="Extract non-private bases without saved fingerprints")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(name="batch_size", description="1 uses CPU; 2-32 require CUDA", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=32)
    async def base_extract_all(self, ctx: ipy.SlashContext, batch_size: int = 1):
        await ctx.defer(ephemeral=True)
        try:
            previous = await BaseExtractionRun.find({"active": False}).sort("-created_at").first_or_none()
            run, created = await self.bot.base_fingerprints.begin_run(str(ctx.author.id), batch_size)
            status = await self.bot.base_fingerprints.status(run)
            status = f"{_fingerprint_status_emoji(run.status)} {status}"
            if created and previous:
                previous_status = await self.bot.base_fingerprints.status(previous)
                status = f"{_fingerprint_status_emoji(previous.status)} {previous_status}\n\n{status}"
            await ctx.send(status, ephemeral=True)
            if created:
                await self.bot.base_fingerprints.publish(run)
        except FingerprintError as error:
            await ctx.send(f"{ERROR_EMOJI} Fingerprint extraction unavailable: {error}.", ephemeral=True)
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not confirm durable extraction work. "
                           "Retry to retrieve its status.", ephemeral=True)

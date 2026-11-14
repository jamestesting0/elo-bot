import functools
import re
import secrets
from datetime import datetime

import interactions as ipy
import validators
from beanie.odm.operators.find.comparison import In
from interactions import utils

from elobot.config.emojis import (
    ERROR_EMOJI,
    IMPORTANT_EMOJI,
    SUCCESS_EMOJI,
)
from elobot.config.game import (
    DISTRICT_CHOICES,
    DISTRICT_CODES_REVERSED,
    DISTRICT_INDEX,
)
from elobot.config.ids import (
    COMMAND_CHANNEL,
    ELO_BLACKLIST_ROLE,
    LVL2_ROLES,
    LVL3_ROLES,
)
from elobot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    ORDINAL_REPRESENTATIONS,
    UNAVAILABLE_IMAGE,
)
from elobot.db import BaseLayout, BaseLink, Match, OverrideCode, Participant
from elobot.discord.mentions import mention_user
from elobot.extensions.match.groups import base_group
from elobot.core.finals import FinalError, is_final_match
from elobot.features.matches.attack_bases import send_bases
from elobot.features.matches.messages import (
    match_message_embeds,
)
from elobot.features.matches.thread_messages import (
    find_match_thread_announcement,
)
from elobot.core.match_results import is_match_finished
from elobot.integrations.files import delete_file
from elobot.integrations.http import download_file
from elobot.features.finals.lifecycle import resolve_final


class MatchBaseCommands:
    @ipy.component_callback(re.compile(r"^submit_bases\|\w+(?:\|[01]\|\d+\|\d+)?$"))
    async def submit_bases_button(self, ctx: ipy.ComponentContext):
        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        parts = ctx.custom_id.split("|")
        token = parts[1]
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match has been canceled.", ephemeral=True)
            return

        if not match.base_submission:
            await ctx.send(f"{ERROR_EMOJI} Base submission is not enabled for this match.", ephemeral=True)
            return

        # figure out which side the author is on
        author_id = str(ctx.author.id)
        if not match.participants or len(match.participants) < 2:
            await ctx.send(f"{ERROR_EMOJI} Invalid match data.", ephemeral=True)
            return

        if is_final_match(match) and (
            author_id not in [participant.user_id for participant in match.participants]
            or ctx.guild_id != match.routing_guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE)
            or match.final_superseded or match.final_result_id is not None
            or len(parts) > 2 and (parts[3] != author_id or int(parts[4]) != match.final_base_page_revision)
        ):
            await ctx.send(f"{ERROR_EMOJI} This base view is unavailable or outdated. Open the current match's controls.",
                           ephemeral=True)
            return

        if is_final_match(match):
            await ctx.send(f"Use `/match base submit match:{match.final_node_id} base_link:<link>` for each base.",
                           ephemeral=True)
            return

        if author_id == match.participants[0].user_id:
            player_index, opp_index = 0, 1
        else:
            player_index, opp_index = 1, 0

        # 10-minute cutoff:
        # - non-war: shared scheduled time (we store in times[0]/times[1])
        # - war:     opponent's scheduled time at their index
        now = int(datetime.now().timestamp())
        times = match.times

        # pick the "non-war" scheduled time (either side; earlier code sets both equal)
        non_war_ts = times[0]
        war_ts = times[opp_index]

        too_late = (
                (not match.is_war and non_war_ts is not None and (non_war_ts - now) < 600) or
                (match.is_war and war_ts is not None and (war_ts - now) < 600)
        )
        if too_late:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(
                f"{ERROR_EMOJI} It is too late to submit now, you can only submit bases 10 minutes before the match.",
                ephemeral=True
            )
            return

        # require opponent to confirm districts first
        # (in this model, final_districts must be fully set)
        if not all(match.final_districts):
            await ctx.send(
                f"{ERROR_EMOJI} Must wait for your opponent to confirm districts first. "
                f"However, we will let the helpers know that you wish to submit bases!",
                ephemeral=True
            )
            # mark intent
            match.participants[player_index].submit_bases = True
            await match.save_changes()
            return

        participant = match.participants[player_index]
        modal = ipy.Modal(title="Base Submission", custom_id=f"submit_bases_modal|{token}")
        for i, district in enumerate(match.final_districts):
            base_data = participant.bases[i]
            if base_data and await BaseLayout.find_one(
                BaseLayout.link == base_data.link,
                BaseLayout.quality != "Private",
            ):
                continue
            modal.add_components(
                ipy.ParagraphText(
                    label=district,
                    value=(base_data.link if base_data else None),
                    placeholder="Put the base link here and make sure it is not expired",
                    max_length=200,
                    custom_id=district,
                    required=False
                )
            )

        if not modal.components:
            await ctx.send(
                f"{ERROR_EMOJI} All your bases are helper bases, so there are no bases you can submit here.",
                ephemeral=True,
            )
            return

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^submit_bases_modal\|\w+(?:\|[01]\|\d+\|\d+)?$"))
    async def submit_bases_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        parts = ctx.custom_id.split("|")
        token = parts[1]

        # load match and base data
        match: Match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Match not found or canceled.", ephemeral=True)
            return

        if is_final_match(match):
            await ctx.send(f"Use `/match base submit match:{match.final_node_id} base_link:<link>` for each base.",
                           ephemeral=True)
            return

        author_id = str(ctx.author.id)
        participant = next((p for p in match.participants if p.user_id == author_id), None)
        if not participant:
            await ctx.send(f"{ERROR_EMOJI} You are not part of this match.", ephemeral=True)
            return

        # validate and update submitted bases
        for index, expected_district in enumerate(match.final_districts):
            if expected_district not in response:
                continue
            base_link = response[expected_district]
            recorded_base = participant.bases[index]
            if recorded_base and recorded_base.link == base_link:
                continue
            if not base_link:
                participant.bases[index] = None
                continue

            # validation checks
            if not validators.url(base_link):
                await ctx.send(f"{ERROR_EMOJI} Invalid URL for the {ORDINAL_REPRESENTATIONS[index]} base.",
                               ephemeral=True)
                return

            if (not base_link.startswith("https://link.clashofclans.com")
                    or "OpenLayout&id" not in base_link.split("=")
                    or base_link.split("%")[1] != "3ACC"):
                await ctx.send(f"{ERROR_EMOJI} The {ORDINAL_REPRESENTATIONS[index]} base link does not look like "
                               f"a Clash of Clans link.", ephemeral=True)
                return

            district = DISTRICT_CODES_REVERSED.get(base_link.split('%')[2])
            if district != expected_district:
                await ctx.send(f"{ERROR_EMOJI} Expected district for the {ORDINAL_REPRESENTATIONS[index]} base is "
                               f"**{expected_district}**, but your link is for **{district}**.", ephemeral=True)
                return

            helper_base = await BaseLayout.find_one(
                BaseLayout.link == base_link,
                BaseLayout.quality != "Private",
            )
            if helper_base:
                await ctx.send(
                    f"{ERROR_EMOJI} The submitted base for **{expected_district}** is a helper's base. "
                    f"You may still use it, but please generate a different link.",
                    ephemeral=True,
                )
                return

            # save base
            participant.bases[index] = BaseLink(link=base_link, image=UNAVAILABLE_IMAGE)

        # persist match update
        await match.save_changes()

        # update pinned match embed
        thread = await self.bot.fetch_channel(match.thread)
        match_msg = await find_match_thread_announcement(thread, match, self.bot.user.id)
        if match_msg is None:
            await ctx.send(
                f"{ERROR_EMOJI} Your bases were saved, but the match message could not be updated. "
                "Please ask a host to notify the helpers.",
                ephemeral=True,
            )
            return
        if not match_msg.pinned:
            await match_msg.pin()

        embed = match_msg.embeds[0]
        note = f"- {mention_user(ctx.author)} has submitted bases."
        if len(embed.fields) < 5:
            embed.add_field(name="**Notes**", value=note)
        elif not re.search(
                rf"(?:<@!?{ctx.author.id}>|users/{ctx.author.id}>\)(?:\*\*)?) has submitted bases\.",
                embed.fields[4].value,
        ):
            embed.fields[4].value += f"\n{note}"

        await match_msg.edit(embeds=match_message_embeds(match_msg.embeds))

        await ctx.send(f"{SUCCESS_EMOJI} Your bases are submitted!", components=[], ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def base_notify(self):
        matches: list[Match] = await Match.find_all().to_list()
        ts = int(datetime.now().timestamp())

        for match in matches:
            token = match.token

            if match.is_war:
                # per-player times
                for player in range(2):
                    if match.times and player < len(match.times):
                        t = match.times[player]
                    else:
                        t = None

                    if t and (t - 600 - ts) < 600:
                        opponent = 0 if player else 1
                        date = datetime.fromtimestamp(t - 600)
                        task = ipy.Task(
                            functools.partial(send_bases, self.bot, token, player, opponent),
                            ipy.DateTrigger(date)
                        )
                        task.start()
            else:
                # single scheduled time
                t = match.matched_time
                if t and (t - 600 - ts) < 600:
                    date = datetime.fromtimestamp(t - 600)

                    task1 = ipy.Task(
                        functools.partial(send_bases, self.bot, token, 0, 1),
                        ipy.DateTrigger(date)
                    )
                    task1.start()

                    task2 = ipy.Task(
                        functools.partial(send_bases, self.bot, token, 1, 0),
                        ipy.DateTrigger(date)
                    )
                    task2.start()

    async def _base_command_match(self, ctx, node_id=None):
        if node_id is None:
            return await Match.find_one(Match.thread == str(ctx.channel.id))
        final = await resolve_final()
        if ctx.guild_id != final.guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("Use this command in the Final's server while eligible.")
        node = final.nodes.get(node_id.upper())
        if node is None or node.match_id is None:
            raise FinalError("Choose a prepared match from `/final match list`.")
        return await Match.get(node.match_id)

    @base_group.subcommand(sub_cmd_name="remove-all", sub_cmd_description="Remove all bases for a match")
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="override_code",
        description="An override code for privileged removal",
        opt_type=ipy.OptionType.STRING,
        min_length=16,
        max_length=16,
        required=False
    )
    async def match_base_remove_all(self, ctx: ipy.SlashContext, player: int, override_code: str = None):
        await ctx.defer(ephemeral=True)

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission check: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants[:2]]
        host_ids = (match.hosts or [None, None])
        is_player = str(ctx.author.id) in participant_ids
        is_host = str(ctx.author.id) in [h for h in host_ids if h]
        if not (is_player or is_host or str(ctx.author.id) == str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to remove the bases.",
                           ephemeral=True)
            return

        # resolve the effective target before checking the opponent's result
        p_idx = 0 if player == 0 else 1
        if is_player:
            p_idx = participant_ids.index(str(ctx.author.id))
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]  # the player whose bases will be removed
        o: Participant = match.participants[o_idx]  # opponent

        started_host = match.hosts[o_idx] if match.is_war else match.hosts[0]
        if started_host:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove bases after the match has started.", ephemeral=True)
            return

        # prevent removal if opponent's result is fully submitted (match finished)
        has_override = await OverrideCode.is_valid("match", override_code)
        if is_match_finished(match, player=o_idx) and not has_override:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove a base once the match has been finished.", ephemeral=True)
            return

        if has_override and not await OverrideCode.consume("match", override_code):
            await ctx.send(
                f"{ERROR_EMOJI} The override code has expired or was already used.",
                ephemeral=True,
            )
            return

        for i in range(len(match.final_districts)):
            base = p.bases[i] if i < len(p.bases) else None
            if not base:
                continue

            district = match.final_districts[i] if i < len(match.final_districts) else None
            if not district:
                continue

            p.bases[i] = None

        # persist match changes
        await match.save_changes()

        # resolve player mention for confirmation
        player_mention = f"<@{p.user_id}>"
        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully removed all bases submitted by {player_mention}!",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove bases for a match")
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="override_code",
        description="An override code for privileged removal",
        opt_type=ipy.OptionType.STRING,
        min_length=16,
        max_length=16,
        required=False
    )
    async def match_base_remove(self, ctx: ipy.SlashContext, player: int, district: str, override_code: str = None):
        await ctx.defer(ephemeral=True)

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants[:2]]
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in participant_ids
                and str(ctx.author.id) not in host_ids
                and str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to remove the bases.",
                           ephemeral=True)
            return

        # district must be assigned in this match
        if district not in (match.final_districts or []):
            await ctx.send(f"{ERROR_EMOJI} **{district}** is not one of the assigned districts!", ephemeral=True)
            return
        district_index = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        # resolve initial target (by slash option): p = selected player, o = opponent
        p_idx = 0 if player == 0 else 1
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]
        o: Participant = match.participants[o_idx]

        # if invoker is a player, force-removal on their own bases
        has_override = await OverrideCode.is_valid("match", override_code)
        if (str(ctx.author.id) in participant_ids and str(ctx.author.id) != str(self.bot.owner.id) and
                not has_override):
            p_idx = participant_ids.index(str(ctx.author.id))
            o_idx = 1 - p_idx
            p, o = match.participants[p_idx], match.participants[o_idx]

        started_host = match.hosts[o_idx] if match.is_war else match.hosts[0]
        if started_host:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove bases after the match has started.", ephemeral=True)
            return

        # block if opponent has a result recorded for this district (match finished for that slot)
        if (((getattr(match, "is_autowin", False) is True) or
             (o.result and len(o.result) > district_index and o.result[district_index])) and
                not has_override):
            await ctx.send(f"{ERROR_EMOJI} You cannot remove a base once the match has been finished.", ephemeral=True)
            return

        # ensure target has a base at this district
        if not (p.bases and len(p.bases) > district_index and p.bases[district_index]):
            await ctx.send(f"{ERROR_EMOJI} <@{p.user_id}> does not have any bases for `{district}`.", ephemeral=True)
            return

        if has_override and not await OverrideCode.consume("match", override_code):
            await ctx.send(
                f"{ERROR_EMOJI} The override code has expired or was already used.",
                ephemeral=True,
            )
            return

        # remove base and persist to Mongo
        p.bases[district_index] = None
        await match.save_changes()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully removed <@{p.user_id}>'s base for **{district}**",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="set-image", sub_cmd_description="Set the scout view of a base")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to set base scout view for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The district of the base",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="The scout view of the base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=True
    )
    async def match_base_set_image(self, ctx: ipy.SlashContext, player: int, district: str, base_ss: ipy.Attachment):
        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        # only host(s) or bot owner may set scout views
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in host_ids) and (str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host is allowed to set scout views.", ephemeral=True)
            return

        # district must be assigned for this match
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not chosen for this match, you cannot submit a base for it.",
                ephemeral=True)
            return

        # resolve participant and district index
        p_idx = 0 if player == 0 else 1
        p: Participant = match.participants[p_idx]
        district_index = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        finished_player = 1 - p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # base must exist to set image
        base = p.bases[district_index] if (p.bases and len(p.bases) > district_index) else None
        if not base:
            await ctx.send(f"{ERROR_EMOJI} No base has been submitted by <@{p.user_id}> for **{district}**.",
                           ephemeral=True)
            return

        # download -> re-upload to owner -> delete temp
        token = secrets.token_hex(8)
        downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
        if not downloaded:
            await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
            return

        file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
        await delete_file(f"temp/{token}.webp")

        # save image URL on the BaseLink
        base.image = file_msg.attachments[0].url
        await match.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have set <@{p.user_id}>'s base scout view for **{district}**",
                       ephemeral=True)

    @base_group.subcommand(sub_cmd_name="set", sub_cmd_description="Set bases for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    async def match_base_set(self, ctx: ipy.SlashContext, player: int, base_link: str, base_ss: ipy.Attachment = None):
        await ctx.defer(ephemeral=True)

        # validate link
        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com")
                or "OpenLayout&id" not in base_link.split("=")
                or base_link.split("%")[1] != "3ACC"):
            await ctx.send(
                f"{ERROR_EMOJI} The link you have provided does not look like a Clash of Clans base link.",
                ephemeral=True
            )
            return

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # disallow hosts command if the invoker is a player; they must use "match base submit"
        if any(str(ctx.author.id) == p.user_id for p in match.participants[:2]):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot use the command as one of the players. Please use "
                f"{self.bot.mention_command(name='match base submit', scope=int(ctx.guild.id))} instead.",
                ephemeral=True
            )
            return

        # only a host or bot owner can set bases
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in host_ids) and (str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host is allowed to set bases.", ephemeral=True)
            return

        # for non-war matches, ensure a time is scheduled
        if (not match.is_war) and not match.matched_time:
            await ctx.send(f"{ERROR_EMOJI} Please schedule a time for the match first.", ephemeral=True)
            return

        # determine district from a link; ensure it’s part of this match
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not chosen for this match, you cannot submit a base for it.",
                ephemeral=True
            )
            return

        # resolve participant and district index
        p_idx = 0 if player == 0 else 1
        p: Participant = match.participants[p_idx]
        d_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        finished_player = 1 - p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # prevent overwriting an existing base
        if p.bases and len(p.bases) > d_idx and p.bases[d_idx]:
            await ctx.send(
                f"{ERROR_EMOJI} A base has already been set for `{district}`. If you wish to set another one, "
                f"please remove the existing one using `/match base remove`.",
                ephemeral=True
            )
            return

        # handle optional screenshot upload
        base_image_url = UNAVAILABLE_IMAGE
        if base_ss:
            token = secrets.token_hex(8)
            downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
            if not downloaded:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
                return

            file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
            await delete_file(f"temp/{token}.webp")
            base_image_url = file_msg.attachments[0].url

        # write into the document and persist
        p.bases[d_idx] = BaseLink(link=base_link, image=base_image_url)
        await match.save_changes()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have set <@{p.user_id}>'s base for **{district}**",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="submit", sub_cmd_description="Submit bases for your match")
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    @ipy.slash_option(name="match", description="Final match, e.g. QF1 (optional in its thread)",
                      opt_type=ipy.OptionType.STRING, autocomplete=True)
    async def match_base_submit(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment = None,
                                match: str = None):
        await ctx.defer(ephemeral=True)

        target_node = match

        # validate link
        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        link_parts = base_link.split("%")
        if (not base_link.startswith("https://link.clashofclans.com/")
                or "OpenLayout&id" not in base_link.split("=")
                or len(link_parts) < 3 or link_parts[1] != "3ACC"
                or link_parts[2] not in DISTRICT_CODES_REVERSED):
            await ctx.send(
                f"{ERROR_EMOJI} The link you have provided does not look like a Clash of Clans base link.",
                ephemeral=True
            )
            return

        try:
            match = await self._base_command_match(ctx, target_node)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if not match.base_submission:
            await ctx.send(f"{ERROR_EMOJI} Base submission is not enabled for this match.", ephemeral=True)
            return

        participant_ids = [p.user_id for p in match.participants]
        author_id = str(ctx.author.id)

        if is_final_match(match) and (ctx.guild_id != match.routing_guild_id
                or ctx.author.has_role(ELO_BLACKLIST_ROLE) or match.final_superseded or match.final_result_id):
            await ctx.send(f"{ERROR_EMOJI} This match is unavailable or finished.", ephemeral=True)
            return

        if author_id not in participant_ids:
            await ctx.send(
                f"{ERROR_EMOJI} Only the players participating the match can submit bases. "
                f"Please ask a host to use `/match base set` if you need help.",
                ephemeral=True
            )
            return

        # determine opponent index
        if author_id == participant_ids[0]:
            opp_index = 1
            author_index = 0
        else:
            opp_index = 0
            author_index = 1

        finished_player = opp_index if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # time gate: allow submission only up to 10 minutes before match
        now_ts = int(datetime.now().timestamp())
        match_time = None
        if match.times:
            match_time = match.times[opp_index] if match.is_war else match.times[0]
        if match_time and (match_time - now_ts) < 600:
            await ctx.send(
                f"{ERROR_EMOJI} It is too late to submit now, you can only submit bases 10 minutes before the match.",
                ephemeral=True
            )
            return

        # district from a link; must be one of the assigned districts
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} The link is for **{district}**, which is not chosen for this match.",
                ephemeral=True
            )
            return

        d_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        # disallow helper bases
        is_helper_base = await BaseLayout.find_one(BaseLayout.link == base_link, BaseLayout.quality != "Private")
        if is_helper_base:
            await ctx.send(
                f"{ERROR_EMOJI} The link is a helper's base. You may still use this base, but please "
                f"generate a different link for it.",
                ephemeral=True
            )
            return

        # prevent duplicate submission for this district
        author_participant = match.participants[author_index]
        previous_base = author_participant.bases[d_idx]
        if is_final_match(match) and previous_base and await BaseLayout.find_one(
                BaseLayout.link == previous_base.link, BaseLayout.quality != "Private"):
            await ctx.send(f"{ERROR_EMOJI} The **{district}** base is a Helper base and cannot be replaced.", ephemeral=True)
            return
        if not is_final_match(match) and author_participant.bases and len(author_participant.bases) > d_idx and author_participant.bases[d_idx]:
            await ctx.send(
                f"{ERROR_EMOJI} A base has already been submitted for `{district}`. If you wish to submit another one, "
                f"please remove the existing one using `/match base remove`.",
                ephemeral=True
            )
            return

        # optional screenshot upload
        base_image_url = UNAVAILABLE_IMAGE
        if base_ss:
            token = secrets.token_hex(8)
            downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
            if not downloaded:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
                return

            file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
            await delete_file(f"temp/{token}.webp")
            base_image_url = file_msg.attachments[0].url

        # persist on the document (no list-size enforcing per your note)
        author_participant.bases[d_idx] = BaseLink(link=base_link, image=base_image_url)
        if is_final_match(match):
            saved = await match.get_pymongo_collection().update_one({
                "_id": match.id, "final_base_page_revision": match.final_base_page_revision,
                "final_superseded": False, "final_result_id": None,
                f"participants.{author_index}.user_id": author_id,
                "times": match.times,
            }, {"$set": {f"participants.{author_index}.bases.{d_idx}": author_participant.bases[d_idx].model_dump()},
                "$inc": {"final_base_page_revision": 1}})
            if not saved.matched_count:
                await ctx.send(f"{ERROR_EMOJI} The match changed. Your base was not saved.", ephemeral=True)
                return
        else:
            await match.save_changes()

        # edit the pinned match message to inform helpers (unchanged behavior)
        try:
            thread = await self.bot.fetch_channel(match.thread) if target_node else ctx.channel
            match_msg = await find_match_thread_announcement(thread, match, self.bot.user.id)
        except ipy.errors.HTTPException:
            await ctx.send(f"{ERROR_EMOJI} Base saved. The match message could not be updated; notify the host.",
                           ephemeral=True)
            return
        if match_msg is None:
            await ctx.send(
                f"{ERROR_EMOJI} Your base was saved, but the match message could not be updated. "
                "Please ask a host to notify the helpers.",
                ephemeral=True,
            )
            return
        try:
            if not match_msg.pinned:
                await match_msg.pin()
            note = f"- {mention_user(ctx.author)} has submitted bases."
            if len(match_msg.embeds[0].fields) < 5:
                match_msg.embeds[0].add_field(name="**Notes**", value=note)
            elif not re.search(
                    rf"(?:<@!?{ctx.author.id}>|users/{ctx.author.id}>\)(?:\*\*)?) has submitted bases\.",
                    match_msg.embeds[0].fields[4].value,
            ):
                match_msg.embeds[0].fields[4].value += f"\n{note}"
            await match_msg.edit(embeds=match_message_embeds(match_msg.embeds))
        except ipy.errors.HTTPException:
            await ctx.send(f"{ERROR_EMOJI} Base saved. The match message could not be updated; notify the host.",
                           ephemeral=True)
            return

        await ctx.send(
            f"{SUCCESS_EMOJI} Base submitted for **{district}**.",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="view", sub_cmd_description="View the bases of the match")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="player",
        description="A player of the match",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ]
    )
    @ipy.slash_option(name="match", description="Final match, e.g. QF1 (optional in its thread)",
                      opt_type=ipy.OptionType.STRING, autocomplete=True)
    async def match_base_view(self, ctx: ipy.SlashContext, player: int = 0, match: str = None):
        await ctx.defer(ephemeral=True)

        try:
            match = await self._base_command_match(ctx, match)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants]
        viewer_is_player = str(ctx.author.id) in participant_ids
        opponent = 0 if player == 1 else 1
        host_id = match.hosts[opponent] if match.is_war else match.hosts[0]
        viewer_is_host = str(ctx.author.id) == host_id
        if not (viewer_is_player or viewer_is_host or str(ctx.author.id) == str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to view the bases.",
                           ephemeral=True)
            return

        # if the viewer is a player, force 'player' to their own index (keeps legacy behavior)
        if viewer_is_player and str(ctx.author.id) != str(self.bot.owner.id):
            player = participant_ids.index(str(ctx.author.id))

        # resolve which participant to display
        p_idx = 0 if player == 0 else 1
        participant: Participant = match.participants[p_idx]

        # for mentions
        player_user = await self.bot.fetch_user(participant.user_id)

        # helper bases data (unchanged JSON file)
        current_base_links = [b.link for b in participant.bases if b]
        helper_docs = await BaseLayout.find(
            In(BaseLayout.link, current_base_links),
            BaseLayout.quality != "Private"
        ).to_list()
        helper_links = {doc.link for doc in helper_docs}

        embeds = []
        index = -1
        for base in participant.bases:
            index += 1
            district = match.final_districts[index] if index < len(match.final_districts) else "Unknown"
            embed = ipy.Embed(
                title=f"**{district}**",
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=COLOR
            )

            if not base:
                embed.set_image(UNAVAILABLE_IMAGE)
                embed.description = f"{IMPORTANT_EMOJI} Base missing.\n"
            else:
                if (base.link in helper_links
                        and viewer_is_player
                        and not viewer_is_host
                        and str(ctx.author.id) != str(self.bot.owner.id)):
                    embed.set_image(UNAVAILABLE_IMAGE)
                    embed.description = "This is a helpers' base"
                else:
                    embed.set_image(base.image)
                    embed.description = base.link

            embeds.append(embed)

        await ctx.send(f"Here are {player_user.mention}'s Bases", embeds=embeds, ephemeral=True)

    @match_base_submit.autocomplete("match")
    @match_base_view.autocomplete("match")
    async def final_base_match_autocomplete(self, ctx: ipy.AutocompleteContext):
        try:
            final = await resolve_final()
            choices = [ipy.SlashCommandChoice(name=node.id, value=node.id) for node in final.nodes.values()
                       if node.match_id and ctx.guild_id == final.guild_id
                       and not ctx.author.has_role(ELO_BLACKLIST_ROLE)
                       and ctx.input_text.lower() in node.id.lower()]
        except FinalError:
            choices = []
        await ctx.send(choices=choices)

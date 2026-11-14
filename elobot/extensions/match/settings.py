import coc
import interactions as ipy

from elobot.config.emojis import (
    ERROR_EMOJI,
    SUCCESS_EMOJI,
)
from elobot.config.game import (
    HOST_CLAN_CHOICES,
)
from elobot.config.ids import (
    LVL2_ROLES,
    MATCH_CHANNEL,
    STAFF_ROLE,
)
from elobot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    GREEN_COLOR,
)
from elobot.core.finals import is_final_match
from elobot.core.match_results import is_match_finished
from elobot.db import Match
from elobot.discord.channel_edits import schedule_channel_name_edit
from elobot.discord.mentions import mention_user
from elobot.extensions.match.groups import set_group
from elobot.features.matches.message_lock import coordinated_match_message
from elobot.features.matches.messages import (
    MatchMessageState,
    publish_match_message,
)
from elobot.features.matches.resources import (
    available_resource_clans_text,
    claim_resource_clan,
    try_reserve_resource_clan,
)
from elobot.features.matches.tips import choose_tip


class MatchSettingsCommands:
    @set_group.subcommand(sub_cmd_name="host", sub_cmd_description="Set a host for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="user",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @coordinated_match_message
    async def match_set_host(self, ctx: ipy.SlashContext, user: ipy.Member):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        if match.is_war:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # require a scheduled time (use the first non-empty timestamp)
        ts = next((t for t in (match.times or []) if t), None)
        if not ts:
            await ctx.send(
                f"{ERROR_EMOJI} Please schedule a time using the command `/match schedule` before setting the host!",
                ephemeral=True
            )
            return

        # host must be a 1v1 helper
        if STAFF_ROLE not in [int(r.id) for r in user.roles]:
            await ctx.send(
                f"{ERROR_EMOJI} {user.mention} is not a 1v1 helper and cannot be selected as the host.",
                ephemeral=True
            )
            return

        # selected host cannot be one of the players
        player_ids = [p.user_id for p in (match.participants or [])[:2]]
        if str(user.id) in {str(pid) for pid in player_ids if pid}:
            await ctx.send(
                f"{ERROR_EMOJI} {user.mention} is a player in the match and cannot be a host!",
                ephemeral=True
            )
            return

        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, 0).binding_filter(), "result_operation": None,
             "participants.0.result": list(match.participants[0].result),
             "participants.1.result": list(match.participants[1].result),
             "is_autowin": {"$ne": True}},
            {"$set": {"hosts.0": str(user.id), "hosts.1": str(user.id)}},
        )
        if write.matched_count != 1:
            await ctx.send(f"{ERROR_EMOJI} The match changed. Check the current host before retrying.",
                           ephemeral=True)
            return
        match.hosts = [str(user.id), str(user.id)]

        # resolve host clan from whichever side is set
        clan_tag = match.host_clans[0]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        # send/edit the thread message (pin)
        thread_embed = ipy.Embed(
            title="**Match Scheduled**",
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**", value=mention_user(user, mention_type="Helper")),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        old_msg = None
        if match.old_msgs[0]:
            old_msg = await ctx.channel.fetch_message(int(match.old_msgs[0]))

        if old_msg:
            if old_msg.embeds and old_msg.embeds[0].title in (
                    "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
            ):
                thread_embed.title = old_msg.embeds[0].title
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=thread_embed, existing_message=old_msg,
            )
        else:
            await publish_match_message(ctx, match, MatchMessageState.capture(match, 0), embed=thread_embed)

        # send/edit the public announcement in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        public_msg = None
        if match.msg:
            public_msg = await channel.fetch_message(int(match.msg))

        if public_msg:
            public_msg.embeds[0].description = (
                "Spectate in the host clan with a TH5 or lower account. "
                f"Host: {mention_user(user, mention_type='Helper')}."
            )
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_msg.embeds[0],
                existing_message=public_msg, public_channel=channel,
            )
        else:
            p1 = await self.bot.fetch_user(int(match.participants[0].user_id))
            p2 = await self.bot.fetch_user(int(match.participants[1].user_id))

            public_embed = ipy.Embed(
                title=thread_embed.title,
                description=(
                    "Spectate in the host clan with a TH5 or lower account. "
                    f"Host: {mention_user(user, mention_type='Helper')}."
                ),
                fields=[
                    ipy.EmbedField(name="**Players**", value=f"{mention_user(p1)} vs. {mention_user(p2)}"),
                    ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                    ipy.EmbedField(
                        name="**Host Clan**",
                        value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                    ),
                ],
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=GREEN_COLOR,
            )

            tip = await choose_tip()
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                content=tip, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully set the host of the match!", ephemeral=True)
        schedule_channel_name_edit(ctx.channel, "🟢")

    @set_group.subcommand(sub_cmd_name="clan", sub_cmd_description="Set a host clan for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @coordinated_match_message
    async def match_set_clan(self, ctx: ipy.SlashContext, clan: str):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # original logic: disallow when "war" (live match) flag is on
        if match.is_war:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # require a scheduled time (use the first non-empty timestamp)
        ts = next((t for t in (match.times or []) if t), None)
        if not ts:
            await ctx.send(
                f"{ERROR_EMOJI} Please schedule a time using the command `/match schedule` "
                f"before setting the host clan!",
                ephemeral=True
            )
            return

        # fetch clan and update host clan in DB
        coc_clan: coc.Clan = await self.coc_client.get_clan(clan)
        reservation = await try_reserve_resource_clan(coc_clan.tag)
        if not reservation or not await claim_resource_clan(
                reservation,
                match,
                0,
                player2_index=1,
                allow_current_owner=True,
        ):
            await ctx.send(
                f"{ERROR_EMOJI} **{coc_clan.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        # resolve the host user if already set on either side
        host_id = None
        if match.hosts:
            host_id = next((h for h in match.hosts if h), None)
        host = await self.bot.fetch_user(int(host_id)) if host_id else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        # thread message (pin/edit)
        embed = ipy.Embed(
            title="**Match Scheduled**",
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**",
                               value=mention_user(host, mention_type="Helper") if host else "Not assigned"),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{coc_clan.name} ({coc_clan.tag})]({coc_clan.share_link})"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        old_msg = None
        if match.old_msgs[0]:
            old_msg = await ctx.channel.fetch_message(int(match.old_msgs[0]))

        if old_msg:
            if old_msg.embeds and old_msg.embeds[0].title in (
                    "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
            ):
                embed.title = old_msg.embeds[0].title
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=embed, existing_message=old_msg,
            )
        else:
            await publish_match_message(ctx, match, MatchMessageState.capture(match, 0), embed=embed)

        # public message in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        public_msg = None
        if match.msg:
            public_msg = await channel.fetch_message(int(match.msg))

        if public_msg:
            # update Host Clan field (index 2) in existing embed
            public_msg.embeds[0].fields[2] = ipy.EmbedField(
                name="**Host Clan**",
                value=f"[{coc_clan.name} ({coc_clan.tag})]({coc_clan.share_link})"
            )
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_msg.embeds[0],
                existing_message=public_msg, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully set the host clan of the match!", ephemeral=True)

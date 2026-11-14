# noqa: SIZE_OK — Discord requires this giveaway extension's command and task registrations to remain together.
import re
import functools
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import anyio
from pymongo.errors import PyMongoError

import interactions as ipy
from common.constants import (
    COLOR, ERROR_EMOJI, FOOTER_IMAGE, FOOTER_TEXT, GIVEAWAY_EMOJI, GOLDPASS_IMAGE, GUILD_IDS, LVL1_ROLES, SUCCESS_EMOJI,
)
from database_models import Giveaway, Player, Season
from common.utils import extract_integer, mention_user
from cogs.leaderboard_commands import get_season_matches


async def mark_giveaway_unavailable(message: ipy.Message, *, ending: bool = False) -> None:
    embed = deepcopy(message.embeds[0]) if message.embeds else ipy.Embed(title="**Giveaway**")
    embed.title = embed.title or "**Giveaway**"
    for marker in ("Confirming", "Unavailable", "Ending", "Ended"):
        embed.title = embed.title.replace(f"`({marker})`", "")
    embed.title = embed.title.rstrip() + (" `(Ending)`" if ending else " `(Unavailable)`")
    embed.fields = [field for field in embed.fields if field.name != "Status"]
    embed.add_field(name="Status", value=(
        "Entries closed. Drawing the winner…" if ending else
        "Giveaway unavailable. Please contact a helper."
    ))
    await message.edit(embed=embed, components=[])


class GiveawayCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self._giveaway_locks: dict[str, anyio.Lock] = {}

    async def end_giveaway(self, msg_id: str, *, reroll: bool = False) -> bool:
        async with self._giveaway_locks.setdefault(msg_id, anyio.Lock()):
            giveaway = await Giveaway.get(msg_id)
            if giveaway is None or (reroll and not giveaway.ended):
                return False
            recovering = giveaway.ended and not reroll
            if recovering and giveaway.draw_id is None:
                return False

            channel = await self.bot.fetch_channel(giveaway.channel_id, force=True)
            if channel is None:
                return False
            message = await channel.fetch_message(msg_id, force=True)
            if message is None:
                return False
            if recovering:
                title = message.embeds[0].title if message.embeds else ""
                if not any(marker in (title or "") for marker in ("`(Ending)`", "`(Unavailable)`")):
                    return False
                current = await Giveaway.get(msg_id)
                if current is None or not current.ended or current.draw_id != giveaway.draw_id:
                    return False
                giveaway = current
            else:
                await mark_giveaway_unavailable(message, ending=True)
                try:
                    if not await giveaway.claim_draw(reroll=reroll):
                        return False
                except PyMongoError:
                    await mark_giveaway_unavailable(message)
                    raise

            embed = deepcopy(message.embeds[0]) if message.embeds else ipy.Embed(title="**Giveaway**")
            embed.title = (embed.title or "**Giveaway**").replace("`(Ending)`", "").replace("`(Unavailable)`", "")
            embed.title = embed.title.rstrip() + " `(Ended)`"
            embed.fields = [field for field in embed.fields if field.name != "Status"]
            details = re.sub(r"\nWinner:.*", "", embed.fields[0].value) if embed.fields else ""
            details = re.sub(r"Entries: \d+", f"Entries: {len(giveaway.entries)}", details).replace("Ends", "Ended")
            winner = giveaway.winner_id
            if winner:
                member = await channel.guild.fetch_member(winner)
                details += f"\nWinner: {mention_user(member, winner)}"
            else:
                details += "\nWinner: None"
            if embed.fields:
                embed.fields[0].value = details
            else:
                embed.add_field(name="Giveaway Details", value=details)
            await message.edit(embed=embed, components=[])

            if recovering:
                return True
            if winner:
                await message.reply(
                    f"{GIVEAWAY_EMOJI} <@{winner}> Congratulations for winning the giveaway! "
                    f"Please claim the price by contacting <@{giveaway.sponsor_id}>.")
            else:
                await message.reply(f"{ERROR_EMOJI} Lmao, no one entered this giveaway!")
            return True

    base = ipy.SlashCommand(name="giveaway", description="", scopes=GUILD_IDS)

    @base.subcommand(sub_cmd_name="create", sub_cmd_description="Create a giveaway")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="giveaway_item",
        description="What will be given away?",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="giveaway_sponsor",
        description="Who will be sponsoring the giveaway?",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="giveaway_channel",
        description="The channel of the giveaway",
        opt_type=ipy.OptionType.CHANNEL,
        channel_types=[
            ipy.ChannelType.GUILD_TEXT
        ],
        required=True
    )
    @ipy.slash_option(
        name="days",
        description="Number of days until the giveaway ends",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        required=False
    )
    @ipy.slash_option(
        name="hours",
        description="Additional hours until the giveaway ends",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        max_value=23,
        required=False
    )
    @ipy.slash_option(
        name="timestamp",
        description="Optional Discord or Unix end timestamp",
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="role_mention",
        description="What role will be pinged?",
        opt_type=ipy.OptionType.ROLE
    )
    @ipy.slash_option(
        name="min_matches",
        description="Minimum matches played to enter the giveaway",
        opt_type=ipy.OptionType.INTEGER
    )
    @ipy.slash_option(
        name="min_elo",
        description="Minimum elo to enter the giveaway",
        opt_type=ipy.OptionType.INTEGER
    )
    @ipy.slash_option(
        name="giveaway_description",
        description="Giveaway description",
        opt_type=ipy.OptionType.STRING
    )
    @ipy.slash_option(
        name="giveaway_thumbnail",
        description="The image URL of the giveaway thumbnail",
        opt_type=ipy.OptionType.STRING
    )
    async def giveaway_create(self, ctx: ipy.SlashContext, giveaway_item: str, giveaway_sponsor: ipy.User,
                              giveaway_channel: ipy.GuildText, days: int | None = None, hours: int | None = None,
                              timestamp: str | None = None,
                              role_mention: ipy.Role | None = None, min_matches: int = 0, min_elo: int = 0,
                              giveaway_description: str = None,
                              giveaway_thumbnail: str = GOLDPASS_IMAGE):
        now = datetime.now(timezone.utc)

        if days is None and hours is None and timestamp is None:
            await ctx.send(f"{ERROR_EMOJI} The giveaway duration must be greater than zero.", ephemeral=True)
            return

        if timestamp is not None:
            end_timestamp = extract_integer(timestamp)
            if not end_timestamp:
                await ctx.send(f"{ERROR_EMOJI} The timestamp you provided is not valid.", ephemeral=True)
                return

            if end_timestamp > 32536799999:
                await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
                return

            try:
                end_datetime = datetime.fromtimestamp(end_timestamp, timezone.utc)
            except (OverflowError, OSError, ValueError):
                await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
                return

            if end_datetime <= now:
                await ctx.send(f"{ERROR_EMOJI} The timestamp must be in the future.", ephemeral=True)
                return
        else:
            days = days or 0
            hours = hours or 0
            if days == 0 and hours == 0:
                await ctx.send(f"{ERROR_EMOJI} The giveaway duration must be greater than zero.", ephemeral=True)
                return

            try:
                end_datetime = now + timedelta(days=days, hours=hours)
            except OverflowError:
                await ctx.send(f"{ERROR_EMOJI} The giveaway duration is out of range.", ephemeral=True)
                return

        end = end_datetime.timestamp()
        await ctx.defer(ephemeral=True)

        embed = ipy.Embed(
            title=f"**{giveaway_item} Giveaway!**",
            description=giveaway_description,
            fields=[
                ipy.EmbedField(
                    name="Giveaway Details",
                    value=f"Ends: <t:{int(end)}:R> \n"
                          f"Host: {mention_user(ctx.author)}\n"
                          f"Sponsor: {mention_user(giveaway_sponsor)}\n"
                          f"Entries: 0\n"
                          f"Requirements: \n"
                          f"1. Played {min_matches}+ Matches\n"
                          f"2. Have {min_elo}+ Elo"
                )
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        if giveaway_thumbnail or giveaway_thumbnail.lower() in ["null", "none"]:
            embed.set_thumbnail(giveaway_thumbnail)

        enter_button = ipy.Button(
            style=ipy.ButtonStyle.SECONDARY,
            label="Enter",
            custom_id="enter_giveaway",
            emoji=GIVEAWAY_EMOJI
        )

        pending = deepcopy(embed)
        pending.add_field(name="Status", value="Opening entries…")
        msg = None
        saved = False
        try:
            if role_mention:
                msg = await giveaway_channel.send(str(role_mention.mention), embed=pending, components=[])
            else:
                msg = await giveaway_channel.send(embed=pending, components=[])
            await Giveaway.insert(Giveaway(
                id=str(msg.id),
                end_timestamp=end,
                sponsor_id=str(giveaway_sponsor.id),
                min_elo=min_elo,
                min_matches=min_matches,
                channel_id=int(giveaway_channel.id),
            ))
            saved = True
            await msg.edit(embed=embed, components=enter_button)
        except (PyMongoError, ipy.errors.HTTPException):
            logging.exception("Giveaway creation could not be confirmed in channel %s", giveaway_channel.id)
            if msg is not None:
                await mark_giveaway_unavailable(msg)
            error_message = ("The giveaway was saved, but its announcement could not be activated." if saved else
                             "The giveaway could not be saved.")
            await ctx.send(f"{ERROR_EMOJI} {error_message} Entries are unavailable.", ephemeral=True)
            return

        await ctx.send(f"{SUCCESS_EMOJI} Giveaway created in {giveaway_channel.mention}!", ephemeral=True)

        task = ipy.Task(functools.partial(self.end_giveaway, str(msg.id)),
                        ipy.DateTrigger(datetime.fromtimestamp(end)))
        task.start()

    @ipy.component_callback("enter_giveaway")
    async def enter_giveaway(self, ctx: ipy.ComponentContext):
        async with self._giveaway_locks.setdefault(str(ctx.message.id), anyio.Lock()):
            await self._enter_giveaway(ctx)

    async def _enter_giveaway(self, ctx: ipy.ComponentContext) -> None:
        giveaway = await Giveaway.get(str(ctx.message.id))
        if giveaway is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch the giveaway.", ephemeral=True)
            return
        if giveaway.ended or giveaway.end_timestamp <= datetime.now().timestamp():
            await ctx.send(f"{ERROR_EMOJI} This giveaway has ended. Entries are closed.", ephemeral=True)
            return

        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)

        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, please register by running "
                           f"the `/player register` command.", ephemeral=True)

            return

        seasons = await Season.find_all().sort("season_number").to_list()
        season_start = int(seasons[-1].end) if seasons else -1
        season_end = datetime.now().timestamp()
        season_matches = get_season_matches(player.matches, season_start, season_end)
        if len(season_matches) < giveaway.min_matches:
            await ctx.send(f"{ERROR_EMOJI} You have not played enough matches this season "
                           f"to enter the giveaway, you can "
                           f"play some matches using the command `/match queue`!\n\n"
                           f"Your Matches: {len(season_matches)}\n"
                           f"Required Matches: {giveaway.min_matches}", ephemeral=True)

            return

        if player.elo < giveaway.min_elo:
            await ctx.send(f"{ERROR_EMOJI} Unfortunately your elo is too low for this giveaway, however you "
                           f"can easily increase your elo by playing more matches!\n\n"
                           f"Your Elo: {player.elo}\n"
                           f"Required Elo: {giveaway.min_elo}", ephemeral=True)

            return

        giveaway = await Giveaway.add_entry(str(ctx.message.id), str(ctx.author.id))
        if giveaway is None:
            current = await Giveaway.get(str(ctx.message.id))
            if current is None or current.ended or current.end_timestamp <= datetime.now().timestamp():
                await ctx.send(f"{ERROR_EMOJI} This giveaway has ended. Entries are closed.", ephemeral=True)
            else:
                await ctx.send(f"{ERROR_EMOJI} You have already entered the giveaway!", ephemeral=True)
            return

        giveaway_entries = len(giveaway.entries)
        ctx.message.embeds[0].fields[0].value = re.sub(r"Entries: \w+", f"Entries: {giveaway_entries}",
                                                       ctx.message.embeds[0].fields[0].value)
        await ctx.message.edit(embed=ctx.message.embeds[0])

        await ctx.send(f"{SUCCESS_EMOJI} You have now entered the giveaway!", ephemeral=True)

    @base.subcommand(sub_cmd_name="reroll", sub_cmd_description="Re-roll a giveaway")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="msg_id",
        description="The message ID of the giveaway",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def giveaway_reroll(self, ctx: ipy.SlashContext, msg_id: str):
        giveaway = await Giveaway.get(msg_id)
        if giveaway is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch the giveaway.", ephemeral=True)

            return

        if not giveaway.ended:
            await ctx.send(f"{ERROR_EMOJI} The giveaway has not ended yet. If you wish to end the giveaway "
                           f"earlier, please run the command `/giveaway end`.", ephemeral=True)

            return

        if not await self.end_giveaway(msg_id, reroll=True):
            await ctx.send(f"{ERROR_EMOJI} The giveaway changed before the reroll could be confirmed.", ephemeral=True)
            return

        await ctx.send(f"{SUCCESS_EMOJI} The giveaway has been re-rolled.", ephemeral=True)

    @base.subcommand(sub_cmd_name="end", sub_cmd_description="End a giveaway")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="msg_id",
        description="The message ID of the giveaway",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def giveaway_end(self, ctx: ipy.SlashContext, msg_id: str):
        giveaway = await Giveaway.get(msg_id)
        if giveaway is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch the giveaway.", ephemeral=True)

            return

        if giveaway.ended and giveaway.draw_id is None:
            await ctx.send(f"{ERROR_EMOJI} The giveaway has already ended. If you wish to re-roll the giveaway "
                           f"earlier, please run the command `/giveaway reroll`.", ephemeral=True)

            return

        if not await self.end_giveaway(msg_id):
            error_message = ("The giveaway has already ended. Use `/giveaway reroll` to draw again." if giveaway.ended else
                             "The giveaway changed before its ending could be confirmed.")
            await ctx.send(f"{ERROR_EMOJI} {error_message}", ephemeral=True)
            return

        await ctx.send(f"{SUCCESS_EMOJI} The giveaway has been ended.", ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(hours=3))
    async def finished_giveaway_delete(self):
        cutoff = (datetime.now() - timedelta(days=6)).timestamp()
        giveaways = await Giveaway.find(Giveaway.end_timestamp <= cutoff, {"ended": True}).to_list()
        for giveaway in giveaways:
            await giveaway.delete()

    @ipy.listen(ipy.events.MessageDelete)
    async def on_message_delete(self, event: ipy.events.MessageDelete):
        giveaway = await Giveaway.get(str(event.message.id))
        if giveaway is not None:
            await giveaway.delete()

    @ipy.Task.create(ipy.IntervalTrigger(minutes=1))
    async def giveaways_end(self):
        giveaways = await Giveaway.find(
            Giveaway.end_timestamp <= datetime.now().timestamp(),
            {"ended": False},
        ).to_list()
        for giveaway in giveaways:
            await self.end_giveaway(giveaway.id)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Giveaway commands loaded")

        await self.giveaways_end()
        await self.finished_giveaway_delete()

        self.giveaways_end.start()
        self.finished_giveaway_delete.start()

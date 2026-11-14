import asyncio
import copy
import re

import interactions as ipy

from elobot.db import Clan, HelperTask, Match, ServerState
from elobot.discord.embeds import embed_edit_payload
from elobot.discord.mentions import mention_user
from elobot.features.base_store.match_adapters import sync_base_store_slots
from elobot.features.base_store.slots import (
    collect_base_store_slot_owners,
    format_clan_availability_status,
)
from elobot.features.matches.resources import free_clan


class MatchDashboardTasks:
    @ipy.Task.create(ipy.IntervalTrigger(seconds=15))
    async def update_clan_availability_embed(self):
        embed_ref = (await ServerState.current()).clan_availability_embed
        if not embed_ref:
            return

        channel = await self.bot.fetch_channel(embed_ref.channel)
        if not channel:
            return

        message = await channel.fetch_message(embed_ref.message)
        if not message:
            return
        loading = message.embeds[0].description in ("Loading…", "Loading...")
        if ((loading and not message.components) or
                any(marker in (message.embeds[0].title or "") for marker in ("`(Confirming)`", "`(Unavailable)`"))):
            return

        await sync_base_store_slots(bot=self.bot)
        matches = await Match.find().to_list()
        slot_owners = collect_base_store_slot_owners(matches)
        clans = await Clan.find_all().to_list()
        clan_availability = {c.tag: c.availability_str for c in clans}
        clan_map = {}
        for c in clans:
            clan_map[c.tag] = format_clan_availability_status(c, slot_owners)

        user_ids = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", "\n".join(clan_map.values()))))
        members = await asyncio.gather(*(channel.guild.fetch_member(user_id) for user_id in user_ids))
        names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}

        embed = copy.deepcopy(message.embeds[0])
        edited = loading
        if loading:
            embed.description = ""

        for field in embed.fields:
            # extract clan tag from field name like "**Clan Name (#TAG)**"
            clan_tag = field.name.split("(")[1].replace(")**", "").strip()
            if clan_tag not in clan_map:
                continue

            thread_id_match = re.search(r"<#(\d+)>", clan_availability[clan_tag])
            if thread_id_match:
                thread_id = thread_id_match.group(1)
                thread = await self.bot.fetch_channel(thread_id)
                if not thread:
                    await free_clan(clan_tag, expected_thread=thread_id)
                    clan = await Clan.find_one(Clan.tag == clan_tag)
                    clan_availability[clan_tag] = clan.availability_str
                    clan_map[clan_tag] = format_clan_availability_status(clan, slot_owners)

            value = re.sub(r"<@!?(\d+)>", lambda mention: names.get(mention.group(1), mention.group(0)),
                           clan_map[clan_tag])
            if field.value == value:
                continue

            field.value = value

            edited = True

        if edited:
            await message.edit(embed=embed_edit_payload(embed))

    @ipy.Task.create(ipy.IntervalTrigger(seconds=15))
    async def update_hosting_queue_embed(self):
        if self._hosting_queue_embed_update_lock.locked():
            return

        async with self._hosting_queue_embed_update_lock:
            embed_ref = (await ServerState.current()).hosting_queue_embed
            if not embed_ref:
                return

            channel = await self.bot.fetch_channel(embed_ref.channel)
            if not channel:
                return

            message = await channel.fetch_message(embed_ref.message)
            if not message:
                return
            if "`(Unavailable)`" in (message.embeds[0].title or ""):
                return

            tasks = await HelperTask.find().sort("-timestamp").to_list()
            tasks_description = ""

            for task in tasks:
                thread = await self.bot.fetch_channel(task.thread)
                if not thread:
                    await task.delete()
                    continue
                discord_timestamp = ipy.Timestamp.fromtimestamp(task.timestamp).format(ipy.TimestampStyles.RelativeTime)
                member = await channel.guild.fetch_member(task.player_id)
                tasks_description += f"<#{task.thread}> {mention_user(member, task.player_id)} {discord_timestamp}\n"

            embed = message.embeds[0]
            title = (embed.title or "").replace("`(Confirming)`", "")
            if embed.description != tasks_description or embed.title != title:
                embed.title = title
                embed.description = tasks_description
                await message.edit(embed=embed_edit_payload(embed))

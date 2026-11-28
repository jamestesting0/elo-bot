import asyncio
import copy
import re

import interactions as ipy

from elo_bot.config.ids import FINAL_CHANNEL
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.db import Clan, HelperTask, Match, ServerState
from elo_bot.discord.embeds import embed_edit_payload
from elo_bot.discord.mentions import mention_user
from elo_bot.features.base_store.match_adapters import sync_base_store_slots
from elo_bot.features.base_store.slots import (
    collect_base_store_slot_owners,
    format_clan_availability_status,
)
from elo_bot.features.embeds.clan_availability import clan_availability_entry, set_clan_availability_entries
from elo_bot.features.matches.resources import free_clan


class MatchDashboardTasks:
    @ipy.Task.create(ipy.IntervalTrigger(seconds=15))
    async def update_clan_availability_embed(self):
        if self._clan_availability_embed_update_lock.locked():
            return

        async with self._clan_availability_embed_update_lock:
            embed_ref = (await ServerState.current()).clan_availability_embed
            if not embed_ref:
                return

            channel = await self.bot.fetch_channel(embed_ref.channel)
            if not channel:
                return

            message = await channel.fetch_message(embed_ref.message)
            if not message:
                return
            current_embed = message.embeds[0]
            loading = current_embed.description in ("Loading…", "Loading...")
            if ((loading and not message.components) or
                    any(marker in (current_embed.title or "") for marker in ("`(Confirming)`", "`(Unavailable)`"))):
                return

            await sync_base_store_slots(bot=self.bot)
            matches = await Match.find().to_list()
            slot_owners = collect_base_store_slot_owners(matches)
            clans = await Clan.find_all().to_list()
            statuses = {clan.tag: format_clan_availability_status(clan, slot_owners) for clan in clans}

            user_ids = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", "\n".join(statuses.values()))))
            members = await asyncio.gather(*(channel.guild.fetch_member(user_id) for user_id in user_ids))
            names = {
                user_id: mention_user(member, user_id, include_wishlist_tab=False)
                for user_id, member in zip(user_ids, members)
            }

            entries = []
            for clan in clans:
                clan_tag = clan.tag
                status = statuses[clan_tag]
                thread_id_match = re.search(r"<#(\d+)>", clan.availability_str)
                if thread_id_match:
                    thread_id = thread_id_match.group(1)
                    thread = await self.bot.fetch_channel(thread_id)
                    if not thread:
                        await free_clan(clan_tag, expected_thread=thread_id)
                        clan = await Clan.find_one(Clan.tag == clan_tag)
                        status = format_clan_availability_status(clan, slot_owners)

                value = re.sub(r"<@!?(\d+)>", lambda mention: names.get(mention.group(1), mention.group(0)), status)
                entries.append(clan_availability_entry(clan, value))

            embed = copy.copy(current_embed)
            set_clan_availability_entries(embed, entries)
            if (embed.description != (current_embed.description or "") or
                    [field.to_dict() for field in embed.fields] != [field.to_dict() for field in current_embed.fields]):
                await edit_channel_message(message, embed=embed_edit_payload(embed))

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
                if isinstance(thread, ipy.ThreadChannel) and thread.parent_id == FINAL_CHANNEL:
                    match = await Match.find_one({"thread": task.thread})
                    if match is None or match.final_superseded or match.final_result_id:
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
                await edit_channel_message(message, embed=embed_edit_payload(embed))

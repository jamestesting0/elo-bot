import anyio
from beanie.odm.operators.update.general import Set

from elobot.extensions.leaderboard import request_leaderboard_player_reload
from elobot.features.matches.resources import free_match_clans
from elobot.features.players.match_choices import clear_match_id_choices, forget_match_id_choice
from elobot.features.hosting.war import result_store
from elobot.features.results.operations import cancel_result_operation
from elobot.features.matches.cleanup import delete_unreserved_match, has_reserved_match_slots
from elobot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
import interactions as ipy
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import GUILD_IDS, LVL1_ROLES, LVL2_ROLES
from elobot.db import Match, Player, RecordedMatch, Season, Helper, BaseLayout
import coc
import json
from elobot.integrations.files import delete_file
from elobot.integrations.http import download_file


class DataCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client

    base = ipy.SlashCommand(name="data", description="", scopes=GUILD_IDS)

    edit_group = base.group(name="edit", description="")

    @edit_group.subcommand(sub_cmd_name="highest-elo", sub_cmd_description="Edit the player's highest elo")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="highest_elo",
        description="The new highest elo of the user",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        required=True
    )
    async def data_edit_highest_elo(self, ctx: ipy.SlashContext, user: ipy.User, highest_elo: int):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(user.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} The user is not registered in the elo system.", ephemeral=True)

            return

        old_highest_elo = player.highest_elo
        player.highest_elo = highest_elo
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} **{user}**'s highest elo is now changed: "
                       f"`{old_highest_elo}` → `{player.highest_elo}`", ephemeral=True)

    @edit_group.subcommand(sub_cmd_name="elo", sub_cmd_description="Edit the player's elo")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="elo",
        description="The new elo of the user",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0,
        required=True
    )
    async def data_edit_elo(self, ctx: ipy.SlashContext, user: ipy.User, elo: int):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(user.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} The user is not registered in the elo system.", ephemeral=True)

            return

        old_elo = player.elo
        player.elo = elo
        if player.elo > player.highest_elo:
            player.highest_elo = player.elo
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} **{user}**'s elo is now changed: "
                       f"`{old_elo}` → `{player.elo}`", ephemeral=True)

    @edit_group.subcommand(sub_cmd_name="priority", sub_cmd_description="Edit player's ability to enter priority queue")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="priority",
        description="Change the player's priority status",
        opt_type=ipy.OptionType.BOOLEAN,
        required=True
    )
    async def data_edit_priority(self, ctx: ipy.SlashContext, user: ipy.User, priority: bool):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(user.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} The user is not registered in the elo system.", ephemeral=True)

            return

        player.priority = priority
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} **{user}**'s priority status is now changed to `{priority}`",
                       ephemeral=True)

    @edit_group.subcommand(sub_cmd_name="tag", sub_cmd_description="Edit a player's in-game tag")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="user",
        description="A user registered in the elo system",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player_tag",
        description="The player's new in-game tag",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def data_edit_tag(self, ctx: ipy.SlashContext, user: ipy.User, player_tag: str):
        await ctx.defer(ephemeral=True)

        player_doc = await Player.find_one(Player.user_id == str(user.id))
        if not player_doc:
            await ctx.send(f"{ERROR_EMOJI} The user is not registered in the elo system.", ephemeral=True)

            return

        try:
            coc_player: coc.Player = await self.coc_client.get_player(player_tag)
        except coc.errors.NotFound:
            await ctx.send(f"{ERROR_EMOJI} The player tag you have provided is invalid.", ephemeral=True)
            return
        except coc.errors.Maintenance:
            await ctx.send(f"{ERROR_EMOJI} Please try again after the maintenance break has ended.",
                           ephemeral=True)
            return

        if coc_player.tag == player_doc.tag:
            await ctx.send(f"{ERROR_EMOJI} The player tag is already linked to the chosen user.",
                           ephemeral=True)

            return

        existing_player = await Player.find_one(Player.tag == coc_player.tag)
        if existing_player:
            await ctx.send(f"{ERROR_EMOJI} The player tag is already linked to another player.",
                           ephemeral=True)

            return

        player_doc.tag = coc_player.tag
        await player_doc.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} The **{user}**'s in-game tag is now changed to "
                       f"`{coc_player.tag}`.", ephemeral=True)

    @edit_group.subcommand(sub_cmd_name="match", sub_cmd_description="Edit a player's match")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="match_id",
        description="The ID of the match to edit",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    @ipy.slash_option(
        name="edited_match",
        description="The edited match as a json file",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=True
    )
    async def data_edit_match(self, ctx: ipy.SlashContext, match_id: str, edited_match: ipy.Attachment):
        await ctx.defer(ephemeral=True)

        recorded_match = await RecordedMatch.find_one(RecordedMatch.id == match_id)
        if not recorded_match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a match with the ID `{match_id}`.", ephemeral=True)

            return

        _, content_type = edited_match.content_type.split("/")
        content_type = content_type.replace("; charset=utf-8", "")

        if content_type != "json":
            await ctx.send(f"{ERROR_EMOJI} The format of the file must be `json`.", ephemeral=True)

            return

        downloaded = await download_file(edited_match.url, f"temp/{ctx.author.id}.{content_type}")
        if not downloaded:
            await ctx.send(f"{ERROR_EMOJI} Failed to download the file.", ephemeral=True)

            return

        try:
            with open(f"temp/{ctx.author.id}.{content_type}", "r") as f:
                edited_match_data = json.load(f)
        except json.decoder.JSONDecodeError as e:
            await ctx.send(f"{ERROR_EMOJI} The JSON formatting is incorrect. Here is the error message:\n"
                           f"```{e}```")
            await delete_file(f"temp/{ctx.author.id}.{content_type}")

            return

        if "players" not in edited_match_data:
            await ctx.send(f"{ERROR_EMOJI} The JSON must contain a 'players' key.", ephemeral=True)
            await delete_file(f"temp/{ctx.author.id}.{content_type}")

            return

        try:
            await recorded_match.update(Set(edited_match_data))
        except Exception as e:
            await ctx.send(f"{ERROR_EMOJI} Failed to update match: {e}", ephemeral=True)
            await delete_file(f"temp/{ctx.author.id}.{content_type}")

            return

        await delete_file(f"temp/{ctx.author.id}.{content_type}")

        await ctx.send(f"{SUCCESS_EMOJI} The match is successfully updated.", ephemeral=True)

    @edit_group.subcommand(sub_cmd_name="player", sub_cmd_description="Edit a player's user ID")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="old_user_id",
        description="The player's old user ID",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="new_user_id",
        description="The player's new user ID",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def data_edit_player(self, ctx: ipy.SlashContext, old_user_id: str, new_user_id: str):
        await ctx.defer(ephemeral=True)

        old_user_id = old_user_id.strip()
        new_user_id = new_user_id.strip()

        if old_user_id == new_user_id:
            await ctx.send(f"{ERROR_EMOJI} The old and new user IDs cannot be the same.", ephemeral=True)

            return

        player_doc = await Player.find_one(Player.user_id == old_user_id)
        if not player_doc:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to find a player with user ID `{old_user_id}`.",
                ephemeral=True
            )

            return

        existing_player = await Player.find_one(Player.user_id == new_user_id)
        if existing_player:
            await ctx.send(
                f"{ERROR_EMOJI} User ID `{new_user_id}` is already linked to another player.",
                ephemeral=True
            )

            return

        recorded_matches = await RecordedMatch.find_all().to_list()

        conflicting_match_ids = [
            match.id
            for match in recorded_matches
            if old_user_id in match.players and new_user_id in match.players
        ]
        if conflicting_match_ids:
            match_preview = ", ".join(f"`{match_id}`" for match_id in conflicting_match_ids[:10])
            if len(conflicting_match_ids) > 10:
                match_preview += ", ..."

            await ctx.send(
                f"{ERROR_EMOJI} Migration aborted because both user IDs already exist in these recorded matches: "
                f"{match_preview}",
                ephemeral=True
            )

            return

        helper_doc = await Helper.find_one(Helper.user_id == old_user_id)
        helper_doc_with_new_id = await Helper.find_one(Helper.user_id == new_user_id)
        base_layouts = await BaseLayout.find(BaseLayout.builder_id == old_user_id).to_list()
        seasons = await Season.find_all().to_list()

        if helper_doc and helper_doc_with_new_id:
            await ctx.send(
                f"{ERROR_EMOJI} User ID `{new_user_id}` is already linked to a helper profile.",
                ephemeral=True
            )

            return

        from elobot.features.finals.administration import migrate_final_identity
        from elobot.features.finals.types import FinalError
        try:
            await migrate_final_identity(old_user_id, new_user_id, str(ctx.author.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        player_doc.user_id = new_user_id
        await player_doc.save_changes()

        if helper_doc:
            helper_doc.user_id = new_user_id
            await helper_doc.save_changes()

        updated_base_layouts = 0
        for base_layout in base_layouts:
            base_layout.builder_id = new_user_id
            await base_layout.save_changes()
            updated_base_layouts += 1

        updated_recorded_matches = 0
        updated_opponents = 0
        for recorded_match in recorded_matches:
            changed = False
            key_updated = False

            players_map = dict(recorded_match.players)

            if old_user_id in players_map:
                old_player_stats = players_map.pop(old_user_id)
                players_map[new_user_id] = old_player_stats
                key_updated = True
                changed = True

            for stats in players_map.values():
                if stats.opponent == old_user_id:
                    stats.opponent = new_user_id
                    updated_opponents += 1
                    changed = True

            if not changed:
                continue

            recorded_match.players = players_map
            await recorded_match.save()
            if key_updated:
                updated_recorded_matches += 1

        updated_seasons = 0
        updated_season_player_entries = 0
        updated_season_builder_entries = 0
        for season in seasons:
            changed = False
            for player_data in season.players:
                if player_data.player_id == old_user_id:
                    player_data.player_id = new_user_id
                    updated_season_player_entries += 1
                    changed = True

            for district_data_list in season.districts.values():
                for district_data in district_data_list:
                    if district_data.builder_id == old_user_id:
                        district_data.builder_id = new_user_id
                        updated_season_builder_entries += 1
                        changed = True

            if changed:
                await season.save()
                updated_seasons += 1

        await ctx.send(
            f"{SUCCESS_EMOJI} Completed player migration:\n"
            f"`Player.user_id`: `{old_user_id}` → `{new_user_id}`\n"
            f"`Helper.user_id`: `{old_user_id}` → `{new_user_id}`\n"
            f"`BaseLayout.builder_id` updated: `{updated_base_layouts}`\n"
            f"`RecordedMatch.players` updated: `{updated_recorded_matches}`\n"
            f"`MatchPlayerStats.opponent` updated: `{updated_opponents}`\n"
            f"`Season` docs updated: `{updated_seasons}`\n"
            f"`Season.players[].player_id` entries updated: `{updated_season_player_entries}`\n"
            f"`Season.districts[].builder_id` entries updated: `{updated_season_builder_entries}`",
            ephemeral=True
        )
        request_leaderboard_player_reload(self.bot)

    export_group = base.group(name="delete", description="")

    @export_group.subcommand(sub_cmd_name="match", sub_cmd_description="Delete a player's match")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="match_id",
        description="The ID of the match to delete",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    async def data_delete_match(self, ctx: ipy.SlashContext, match_id: str):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)
        from elobot.features.finals.administration import guard_result_deletion
        from elobot.features.finals.types import FinalError
        try:
            await guard_result_deletion(match_id)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        recorded_match = await RecordedMatch.find_one(RecordedMatch.id == match_id)
        if not recorded_match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a match with the ID `{match_id}`.", ephemeral=True)

            return

        async def remove_record(record: RecordedMatch) -> None:
            match_reference = record.to_ref()
            for player_id, stats in record.players.items():
                await Player.get_pymongo_collection().update_one(
                    {"user_id": player_id, "matches": match_reference},
                    {"$inc": {"elo": -stats.elo_change}, "$pull": {"matches": match_reference}},
                )
            await record.delete()
            await forget_match_id_choice(self.bot, record.id)
            await ctx.send(f"{SUCCESS_EMOJI} The match is successfully deleted.", ephemeral=True)
            request_leaderboard_player_reload(self.bot)

        record_lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(f"record:{match_id}", anyio.Lock())
        async with record_lock:
            current = await RecordedMatch.find_one(RecordedMatch.id == match_id)
            if not current or current.operation_key != recorded_match.operation_key:
                await ctx.send(f"{ERROR_EMOJI} This match record was already deleted or replaced.", ephemeral=True)
                return
            recorded_match = current
            association = {
                "result_operation.record.id": recorded_match.id,
                "result_operation.record.operation_key": recorded_match.operation_key,
            }
            manual_key = recorded_match.manual_submission_key
            related_match = await Match.find_one(association) if recorded_match.operation_key and not manual_key else None
            try:
                if related_match:
                    lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(related_match.token, anyio.Lock())
                    async with lock, PLAYER_DATA_BOUNDARY.write(data_generation):
                        current = await RecordedMatch.find_one(RecordedMatch.id == match_id)
                        if not current or current.operation_key != recorded_match.operation_key:
                            await ctx.send(f"{ERROR_EMOJI} This match record was already deleted or replaced.", ephemeral=True)
                            return
                        related_match = await Match.find_one(association)
                        if related_match:
                            await cancel_result_operation(related_match, current)
                        await remove_record(current)
                elif manual_key:
                    lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(manual_key, anyio.Lock())
                    async with lock, PLAYER_DATA_BOUNDARY.write(data_generation):
                        current = await RecordedMatch.find_one(RecordedMatch.id == match_id)
                        if not current or current.operation_key != recorded_match.operation_key:
                            await ctx.send(f"{ERROR_EMOJI} This match record was already deleted or replaced.", ephemeral=True)
                            return
                        await remove_record(current)
                else:
                    async with PLAYER_DATA_BOUNDARY.write(data_generation):
                        await remove_record(recorded_match)
            except PlayerDataReset as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    export_group = base.group(name="export", description="")

    @export_group.subcommand(sub_cmd_name="match", sub_cmd_description="Export a player's match data as json")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="match_id",
        description="The ID of the match to export",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    async def data_export_match(self, ctx: ipy.SlashContext, match_id: str):
        await ctx.defer(ephemeral=True)

        recorded_match = await RecordedMatch.find_one(RecordedMatch.id == match_id)
        if not recorded_match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a match with the ID `{match_id}`.", ephemeral=True)

            return

        match_data = recorded_match.model_dump(mode="json")

        filename = f"temp/{match_id}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(match_data, f, indent=4)

        file = ipy.File(file=filename)
        await ctx.send(file=file, ephemeral=True)
        await delete_file(filename)

    reset_group = base.group(name="reset", description="")

    @reset_group.subcommand(sub_cmd_name="elo", sub_cmd_description="Reset all players' elo")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="elo",
        description="The elo of players after the reset",
        opt_type=ipy.OptionType.INTEGER,
        min_value=0
    )
    async def data_reset_elo(self, ctx: ipy.SlashContext, elo: int = 1000):
        await ctx.defer(ephemeral=True)

        try:
            async with PLAYER_DATA_BOUNDARY.reset():
                await Player.find_all().update(Set({Player.elo: elo}))

                await ctx.send(f"{SUCCESS_EMOJI} All players' elo is now set to `{elo}`.", ephemeral=True)
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @reset_group.subcommand(sub_cmd_name="all", sub_cmd_description="Reset all data of the players")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def data_reset_all(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)
        from elobot.features.finals.administration import guard_full_reset
        from elobot.features.finals.types import FinalError
        try:
            async with PLAYER_DATA_BOUNDARY.reset():
                await guard_full_reset()
                matches = await Match.find_all().to_list()
                for match in matches:
                    if has_reserved_match_slots(match):
                        await ctx.send(
                            f"{ERROR_EMOJI} Match `{match.token}` still has reserved stored bases. "
                            "Use **Undo** on its stored-base message before retrying `/data reset all`. "
                            "Player data was not reset.",
                            ephemeral=True,
                        )
                        return
                for match in matches:
                    if not await delete_unreserved_match(match, allow_pending_operations=True):
                        await ctx.send(
                            f"{ERROR_EMOJI} Match <#{match.thread}> changed during reset. "
                            "Player data was not reset. Use **Undo** on any stored bases before trying again.",
                            ephemeral=True,
                        )
                        return
                    await free_match_clans(match)

                # Reset all player data
                await Player.find_all().update(Set({
                    Player.elo: 1000,
                    Player.highest_elo: 1000,
                    Player.wins: 0,
                    Player.avg_stars: 0.0,
                    Player.avg_destruction: 0.0,
                    Player.opponent_avg_stars: 0.0,
                    Player.opponent_avg_destruction: 0.0,
                    Player.last_cancel: None,
                    Player.matches: [],
                    Player.last_challenged: []
                }))

                # Clear all matches in MongoDB
                await RecordedMatch.delete_all()
                await clear_match_id_choices(self.bot)

                await ctx.send(f"{SUCCESS_EMOJI} All data of the players are now reset.", ephemeral=True)
                request_leaderboard_player_reload(self.bot)
        except (PlayerDataReset, FinalError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Data commands loaded")

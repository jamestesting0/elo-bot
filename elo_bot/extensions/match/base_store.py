import re
from typing import assert_never

import coc
import interactions as ipy

import elo_bot.features.base_store.live_service as live_base_service
import elo_bot.features.base_store.presentation as base_store_presentation
import elo_bot.features.base_store.publication as base_store_publication
import elo_bot.features.base_store.storage as base_store_storage
import elo_bot.features.hosting.war.war_start as war_start
from elo_bot.config.emojis import (
    ERROR_EMOJI,
    SUCCESS_EMOJI,
)
from elo_bot.config.game import (
    BASE_STORE_SLOTS,
    HOST_CLAN_CHOICES,
)
from elo_bot.config.ids import (
    LVL2_ROLES,
)
from elo_bot.db import Match, Participant
from elo_bot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from elo_bot.features.base_store.match_adapters import clear_participant_base_store, sync_base_store_slots
from elo_bot.extensions.match.groups import base_group
from elo_bot.features.base_store.messages import BaseStoreReleaseReason
from elo_bot.features.base_store.models import (
    BaseStorePreparationFailure,
    BaseStorePreparationOutcome,
    BaseStorePreparationRequest,
    BaseStorePresentationRequest,
    PreparedBaseStore,
    SelfBaseStoreRequest,
    StoredBaseActionRequest,
    StoredBaseControlOutcome,
    StoredBaseControlState,
    bind_legacy_store_message,
    stored_base_matches,
)
from elo_bot.features.base_store.raid_task import store_before_raid_weekend
from elo_bot.features.base_store.start import StoredStartState
from elo_bot.features.hosting.war.confirmation import PendingBaseConfirmation, confirm_pending_opponent_bases
from elo_bot.features.matches.attack_bases import choose_bases
from elo_bot.features.matches.resources import (
    ResourceClanReservation,
    ResourceMatchSideReservation,
    is_clan_available,
    try_reserve_resource_clan,
    try_reserve_resource_match_side,
)
from elo_bot.core.match_results import is_match_finished


class MatchBaseStoreCommands:
    async def _prepare_base_store(
            self,
            request: BaseStorePreparationRequest,
    ) -> BaseStorePreparationOutcome:
        return await base_store_storage.prepare_base_store(self, request)

    async def _base_store_sync_slots(self, clan_tag: str | None = None) -> dict[str, list[str]]:
        return await sync_base_store_slots(clan_tag, bot=self.bot)

    async def _base_store_choose_bases(self, match: Match, player: int) -> None:
        opponent = 1 - player
        player_obj = await self.bot.fetch_user(match.participants[player].user_id)
        opponent_obj = await self.bot.fetch_user(match.participants[opponent].user_id)
        await choose_bases(match.token, player_obj, opponent_obj, True)

    async def _base_store_clear_participant(
            self,
            match: Match,
            player: int,
            reset_stored_flag: bool = False,
            *,
            reason: BaseStoreReleaseReason,
    ) -> bool:
        return await clear_participant_base_store(
            match, player, reset_stored_flag=reset_stored_flag, bot=self.bot, reason=reason,
        )

    async def _base_store_try_reserve_side(
            self,
            token: str,
            player: int,
    ) -> ResourceMatchSideReservation | None:
        return await try_reserve_resource_match_side(token, player)

    async def _base_store_try_reserve_clan(self, clan_tag: str) -> ResourceClanReservation | None:
        return await try_reserve_resource_clan(clan_tag)

    async def _base_store_is_clan_available(self, clan_tag: str) -> bool:
        return await is_clan_available(clan_tag)

    async def _base_store_confirm_pending(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
    ) -> bool:
        return await confirm_pending_opponent_bases(
            self.bot,
            ctx,
            PendingBaseConfirmation(match=state.match, player=state.player),
        )

    async def _base_store_start_local(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
            clan: coc.Clan,
    ) -> ipy.Message | None:
        return await war_start.start_war(
            self.bot,
            ctx,
            war_start.WarStart(
                match=state.match,
                player=state.player,
                host=self.bot.user,
                clan=clan,
                coc_client=self.coc_client,
                reply=war_start.StoredWarStartReply(
                    slot=state.slot,
                    slot_instruction="",
                    helper_hosted=True,
                ),
                stored_start=StoredStartState.capture_stores(state.match, (state.player,)),
            ),
        )

    @base_group.subcommand(sub_cmd_name="store", sub_cmd_description="Store bases in a clan for raid weekend")
    @ipy.slash_option(
        name="player",
        description="The player to store bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used to store the bases",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="slot",
        description="The slot in which the bases are stored",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Slot 1", value="Slot 1"),
            ipy.SlashCommandChoice(name="Slot 2", value="Slot 2"),
        ],
        required=True
    )
    async def match_base_store(self, ctx: ipy.SlashContext, player: int, clan: str, slot: str):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # disallow storing bases if the invoker is one of the players (unless a bot owner)
        is_player = any(str(ctx.author.id) == p.user_id for p in match.participants[:2])
        if is_player and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True)
            return

        # resolve player/opponent indices
        p_idx = 0 if player == 0 else 1
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]
        o: Participant = match.participants[o_idx]
        player_id, opponent_id = p.user_id, o.user_id

        if p.base_store is not None:
            reserved_clan = await self.coc_client.get_clan(p.base_store.clan_tag)
            presentation = BaseStorePresentationRequest(
                match=match,
                player=p_idx,
                helper_id=str(ctx.author.id),
                clan=reserved_clan,
                bases_stored=False,
                failure_reason="A stored-slot reservation already exists.",
            )
            if await base_store_publication.present_retained_base_store(ctx, presentation):
                await ctx.send(
                    f"{ERROR_EMOJI} The bases for <@{player_id}> already reserve a slot. "
                    "Its Undo control has been restored.",
                    ephemeral=True,
                )
            return
        if p.opponent_bases_stored:
            await ctx.send(f"{ERROR_EMOJI} The bases for <@{player_id}> are already stored.", ephemeral=True)
            return

        finished_player = p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        if slot not in BASE_STORE_SLOTS:
            await ctx.send(f"{ERROR_EMOJI} `{slot}` is not a valid base-store slot.", ephemeral=True)
            return

        clan_obj = await self.coc_client.get_clan(clan)
        preparation = await self._prepare_base_store(BaseStorePreparationRequest(
            thread_id=str(ctx.channel.id),
            player=p_idx,
            clan=clan_obj,
            slot=slot,
        ))
        match preparation:
            case BaseStorePreparationFailure(message=message):
                await ctx.send(message, ephemeral=True)
                return
            case PreparedBaseStore(match=prepared_match):
                match = prepared_match
            case unreachable:
                assert_never(unreachable)

        presentation = BaseStorePresentationRequest(
            match=match,
            player=player,
            helper_id=str(ctx.author.id),
            clan=clan_obj,
            bases_stored=False,
        )

        if not await base_store_publication.publish_base_store_message(
                ctx, presentation, base_store_presentation.build_manual_store_components(presentation, slot),
        ):
            return
        await ctx.send(
            f"Please store the bases in the clan and slot you have selected.\n\n"
            f"**<@{opponent_id}>'s Bases**\n{base_store_presentation.format_opponent_bases(presentation)}",
            suppress_embeds=True,
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="autostore", sub_cmd_description="Store bases with Helper for raid weekend")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.slash_option(
        name="player",
        description="The player to store bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    async def match_base_autostore(self, ctx: ipy.SlashContext, player: int):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        p_idx = 0 if player == 0 else 1
        is_player = any(str(ctx.author.id) == p.user_id for p in match.participants[:2])
        if is_player and str(ctx.author.id) != str(self.bot.owner.id) and (
            str(ctx.author.id) == match.participants[p_idx].user_id
            or not any(ctx.author.has_role(role) for role in LVL2_ROLES)
        ):
            await ctx.send(f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True)
            return

        await base_store_storage.store_self_bases(
            self,
            ctx,
            SelfBaseStoreRequest(match=match, player=p_idx, helper_id=str(ctx.author.id)),
        )

    @ipy.component_callback(re.compile(r"undo_base_storage\|\w+\|\d+(?:\|\w+)?$"))
    async def undo_base_storage_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        _, token, player, *reservation = ctx.custom_id.split("|")

        # fetch match by token (replaces matches.json)
        match: Match | None = await Match.find_one(Match.token == token)
        if not match:
            failure = f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return

        idx = int(player)
        if idx >= len(match.participants):
            await ctx.send(f"{ERROR_EMOJI} Invalid player index.", ephemeral=True)
            return

        request = StoredBaseActionRequest(
            token, idx, reservation[0] if reservation else None, int(ctx.message.id),
        )
        if not stored_base_matches(match, request):
            failure = f"{ERROR_EMOJI} This message no longer has an active stored-base reservation."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return
        if not await bind_legacy_store_message(match, idx, int(ctx.message.id)):
            failure = f"{ERROR_EMOJI} The reservation changed; removal could not be confirmed."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return

        # unset the stored flag for the selected player
        if not await clear_participant_base_store(
                match, idx, reset_stored_flag=True, bot=self.bot, reason="undone",
        ):
            await ctx.send(f"{ERROR_EMOJI} The reservation changed; removal could not be confirmed.", ephemeral=True)
            return

        await base_store_publication.mark_store_status(
            ctx.message, "**Base Storage Undone**", context=ctx,
        )
        await ctx.send(
            f"{SUCCESS_EMOJI} This action has been undone, please make sure that the bases are removed from the clans.",
            ephemeral=True
        )

    @ipy.component_callback(re.compile(r"start_match\|\d+\|[^|]+\|[^|]+(?:\|\w+)?$"))
    async def start_match_button(self, ctx: ipy.ComponentContext):
        _, player, clan, slot, *reservation = ctx.custom_id.split("|")
        await self._start_war(
            ctx, int(player), clan, host=ctx.author, slot=slot,
            stored_reservation_id=reservation[0] if reservation else None,
        )

    @ipy.component_callback(re.compile(r"helper_stored_start\|\w+\|\d+(?:\|\w+)?$"))
    async def helper_stored_start_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        _, token, player_text, *reservation = ctx.custom_id.split("|")
        await base_store_storage.start_stored_bases(
            self,
            ctx,
            StoredBaseActionRequest(token, int(player_text), reservation[0] if reservation else None, int(ctx.message.id)),
        )

    @ipy.component_callback(re.compile(r"helper_stored_undo\|\w+\|\d+(?:\|\w+)?$"))
    async def helper_stored_undo_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        _, token, player_text, *reservation = ctx.custom_id.split("|")
        await base_store_storage.undo_stored_bases(
            self,
            ctx,
            StoredBaseActionRequest(token, int(player_text), reservation[0] if reservation else None, int(ctx.message.id)),
        )

    async def _resolve_stored_base_control(
            self,
            token: str,
            player: int,
    ) -> StoredBaseControlOutcome:
        return await base_store_storage.resolve_stored_base_control(
            StoredBaseActionRequest(token=token, player=player)
        )

    async def store_self_bases(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
    ) -> None:
        await base_store_storage.store_self_bases(
            self,
            ctx,
            SelfBaseStoreRequest(match=match, player=player),
        )

    async def store_self_live_bases(self, ctx: ipy.SlashContext, match: Match) -> None:
        await live_base_service.store_self_live_bases(self, ctx, match)

    @ipy.Task.create(ipy.CronTrigger(cron="0 6 * * 5"))  # one hour before raid weekend, Friday UTC
    async def raid_weekend_auto_store(self) -> None:
        await store_before_raid_weekend(self)

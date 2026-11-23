import re

import interactions as ipy

import elo_bot.extensions.base.groups as groups
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.game import DISTRICT_CHOICES
from elo_bot.config.ids import (
    LVL1_ROLES,
    LVL3_ROLES,
    WHITELISTED_STAFFS,
)
from elo_bot.db import BaseLayout
from elo_bot.discord.checks import has_any, has_any_id
from elo_bot.discord.members import member_guild
from elo_bot.discord.pagination import InvalidPageError, build_page_jump_modal, parse_page_number
from elo_bot.extensions.base.groups import BASE_QUALITY_CHOICES
from elo_bot.features.bases.layouts.list_view import BaseListPayload, BaseListState, base_defense_card, base_list_card
from elo_bot.features.bases.layouts.queries import _one_listed_base, _public_list_query
from elo_bot.features.bases.layouts.ui import (
    _build_base_list_components,
    _parse_base_list_state,
    base_embed_names,
    construct_base_embed,
)


def _owns_base_list(ctx: ipy.ComponentContext | ipy.ModalContext) -> bool:
    interaction = ctx.message.interaction or getattr(ctx.message, "interaction_metadata", None)
    return bool(ctx.author and interaction and int(ctx.author.id) == int(interaction.user))


class BaseListCommands:
    base = groups.base

    async def _base_list_payload(
            self, ctx: ipy.SlashContext | ipy.ComponentContext | ipy.ModalContext,
            base: BaseLayout, state: BaseListState, *, defense_page: int = 0,
    ) -> BaseListPayload:
        rows = _build_base_list_components(
            state.district, state.index, state.embed_type, state.quality, state.builder_id, state.total,
        )
        message = ctx.message if isinstance(ctx, (ipy.ComponentContext, ipy.ModalContext)) else None
        has_card = message and any(isinstance(item, ipy.ContainerComponent) for item in message.components or [])
        guild = await member_guild(ctx)
        if state.embed_type == "Defense Log":
            card = await base_defense_card(guild, base, defense_page)
        else:
            names = await base_embed_names(guild, base, state.embed_type)
            embed = construct_base_embed(base, state.embed_type, names=names)
            if not has_card:
                return {"embed": embed, "components": [*rows]}
            card = base_list_card(embed, base.image_url)

        payload: BaseListPayload = {"components": [card, *rows], "allowed_mentions": ipy.AllowedMentions.none()}
        if message and message.embeds:
            payload["embeds"] = []
        return payload

    @base.subcommand(sub_cmd_name="list", sub_cmd_description="List bases of a certain district")
    @ipy.check(has_any(ipy.has_any_role(*LVL3_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES
    )
    @ipy.slash_option(
        name="embed_type",
        opt_type=ipy.OptionType.STRING,
        description="The type of embed to display",
        choices=[
            ipy.SlashCommandChoice(name="Performance", value="Performance"),
            ipy.SlashCommandChoice(name="Defense Log", value="Defense Log")
        ]
    )
    @ipy.slash_option(
        name="base_quality",
        description="The quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES
    )
    @ipy.slash_option(
        name="builder",
        description="The builder of the base",
        opt_type=ipy.OptionType.USER
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def base_list(self, ctx: ipy.SlashContext, district: str = "Any", embed_type: str = "Performance",
                        base_quality: str = "Any", builder: str = None, hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        if not ctx.author.has_any_role(LVL1_ROLES):
            builder = ctx.author

        button_builder_state = str(builder.id) if builder else "Any"
        query = _public_list_query(self.bot, district, base_quality, button_builder_state)
        base_doc, total = await _one_listed_base(query, 0)

        # handle empty results
        if base_doc is None:
            msg = f"{ERROR_EMOJI} No bases found for `{district}`"
            if base_quality != "Any":
                msg += f" with **{base_quality}** quality"
            if builder:
                msg += f" built by {builder.mention}"
            msg += "."

            await ctx.send(msg, ephemeral=hidden)
            return

        state = BaseListState(district, 0, embed_type, base_quality, button_builder_state, total)
        await ctx.send(ephemeral=hidden, **await self._base_list_payload(ctx, base_doc, state))

    @ipy.component_callback(re.compile(r"^base_right\|.*\|\d+\|.*\|.*$"))
    async def base_right_button(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        _, district, index_str, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        index = int(index_str) + 1

        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        state = BaseListState(district, index, embed_type, base_quality, builder_id, total)
        await ctx.edit_origin(**await self._base_list_payload(ctx, base, state))

    @ipy.component_callback(re.compile(r"^base_left\|.*\|\d+\|.*$"))
    async def base_left_button(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        _, district, index_str, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        index = int(index_str) - 1

        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        state = BaseListState(district, index, embed_type, base_quality, builder_id, total)
        await ctx.edit_origin(**await self._base_list_payload(ctx, base, state))

    @ipy.component_callback(re.compile(r"^base_defense\|[^|]+\|[0-9]+\|(newer|older)$"))
    async def base_defense_page(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return

        _, base_id, page, _ = ctx.custom_id.split("|")
        district, index, embed_type, base_quality, builder_id = _parse_base_list_state(ctx.message.components)
        if embed_type != "Defense Log":
            await ctx.send(f"{ERROR_EMOJI} Open the Defense Log to browse defenses.", ephemeral=True)
            return

        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None or str(base.id) != base_id:
            await ctx.send(f"{ERROR_EMOJI} This base is no longer on this page.", ephemeral=True)
            return

        await ctx.defer(edit_origin=True)
        state = BaseListState(district, index, embed_type, base_quality, builder_id, total)
        await ctx.edit_origin(**await self._base_list_payload(ctx, base, state, defense_page=int(page)))

    @ipy.component_callback(re.compile(r"^base_page\|.*\|.*\|.*\|.*$"))
    async def base_page_button(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        modal_custom_id = ctx.custom_id.replace("base_page|", "base_page_modal|", 1)
        await ctx.send_modal(build_page_jump_modal(modal_custom_id))

    @ipy.modal_callback(re.compile(r"^base_page_modal\|.*\|.*\|.*\|.*$"))
    async def base_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        _, district, embed_type, base_quality, builder_id = ctx.custom_id.split("|")
        query = _public_list_query(self.bot, district, base_quality, builder_id)

        total = await query.count()
        if total == 0:
            await ctx.send(f"{ERROR_EMOJI} There are no matching bases anymore.", ephemeral=True)
            return

        try:
            page = parse_page_number(response["page"], total)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        index = page - 1
        base, total = await _one_listed_base(query, index, total=total)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        state = BaseListState(district, index, embed_type, base_quality, builder_id, total)
        await ctx.edit("@original", **await self._base_list_payload(ctx, base, state))

    @ipy.component_callback("base_list_district_select")
    async def base_list_district_select(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        district = ctx.values[0]
        _, _, embed_type, base_quality, builder_id = _parse_base_list_state(ctx.message.components)
        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, 0)
        if base is None:
            msg = f"{ERROR_EMOJI} No bases found for `{district}`"
            if base_quality != "Any":
                msg += f" with **{base_quality}** quality"
            if builder_id != "Any":
                msg += f" built by <@{builder_id}>"
            msg += "."
            await ctx.send(msg, ephemeral=True)
            return

        state = BaseListState(district, 0, embed_type, base_quality, builder_id, total)
        await ctx.edit_origin(**await self._base_list_payload(ctx, base, state))
        await ctx.send(f"{SUCCESS_EMOJI} The district type is changed to `{district}`.", ephemeral=True)

    @ipy.component_callback("base_list_embed_type_select")
    async def base_list_embed_type_select(self, ctx: ipy.ComponentContext):
        if not _owns_base_list(ctx):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.",
                           ephemeral=True)
            return

        embed_type = ctx.values[0]
        district, index, _, base_quality, builder_id = _parse_base_list_state(ctx.message.components)
        query = _public_list_query(self.bot, district, base_quality, builder_id)
        base, total = await _one_listed_base(query, index)
        if base is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        state = BaseListState(district, index, embed_type, base_quality, builder_id, total)
        await ctx.edit_origin(**await self._base_list_payload(ctx, base, state))
        await ctx.send(f"{SUCCESS_EMOJI} The embed type is changed to `{embed_type}`.", ephemeral=True)

import os
import re
import secrets
from datetime import datetime

import interactions as ipy
import validators

import elo_bot.extensions.base.groups as groups
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.game import DISTRICT_CHOICES, DISTRICT_CODES_REVERSED
from elo_bot.config.presentation import (
    UNAVAILABLE_IMAGE,
)
from elo_bot.db import BaseLayout, Player
from elo_bot.discord.members import member_guild
from elo_bot.features.bases.fingerprints.images import attachment_identity, download_base_screenshot
from elo_bot.features.bases.fingerprints.ingestion import replace_source
from elo_bot.db.fingerprint_types import PIPELINE_ID, ExtractionState
from elo_bot.features.bases.layouts.private import (
    clear_private_base_preset,
    find_private_base_action,
    update_private_base_message,
)
from elo_bot.features.bases.layouts.ui import base_embed_names, construct_base_embed
from elo_bot.features.bases.screenshots import capture_base_screenshot
from elo_bot.integrations.files import delete_file


class PrivateBaseCommands:
    private_group = groups.private_group

    @private_group.subcommand(sub_cmd_name="add", sub_cmd_description="Add a private base")
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    async def base_private_add(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment | None = None):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered. Please register first.", ephemeral=True)
            return
        
        player_base_count = await BaseLayout.find(
            BaseLayout.builder_id == str(ctx.author.id),
            BaseLayout.quality == "Private"
        ).count()
        base_count_limit = 10 if player.elo < 1200 else 20
        if player_base_count >= base_count_limit:
            await ctx.send(f"{ERROR_EMOJI} You have reached the maximum number of private bases ({base_count_limit}). "
                           f"Please remove some of your private bases before adding a new one.", ephemeral=True)

            return

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or
                (len(base_link.split("%")) > 1 and base_link.split("%")[1] != "3ACC")):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)
            return

        helper_base = await BaseLayout.find_one(
            BaseLayout.link == base_link, BaseLayout.quality != "Private"
        )
        if helper_base:
            await ctx.send(f"{ERROR_EMOJI} This base link is already registered as a helper base.", ephemeral=True)
            return

        # handle image upload
        token = secrets.token_hex(8)
        temp_path = f"temp/{token}.webp"

        downloaded = False
        file_msg = None
        try:
            if base_ss is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, base_ss.url, temp_path)
                if not downloaded:
                    return
            else:
                downloaded = await capture_base_screenshot(
                    base_link, str(ctx.author.id), temp_path, ctx=ctx, keep_secret=True,
                )
            if downloaded:
                file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
                if not file_msg.attachments:
                    await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
                    return
        except ipy.errors.HTTPException:
            if base_ss is not None:
                raise
            await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. No base was changed.", ephemeral=True)
            return
        finally:
            if downloaded or os.path.exists(temp_path):
                await delete_file(temp_path)

        # update database
        expiration_ts = datetime.now().timestamp() + 15 * 86400
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        
        # check if base already exists (even if private)
        existing_base = await BaseLayout.find_one(
            BaseLayout.link == base_link, 
            BaseLayout.quality == "Private"
        )
        if existing_base:
            if file_msg is None:
                await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. Your previous base was kept.",
                               ephemeral=True)
                return
            await replace_source(existing_base, file_msg.attachments[0].url, district,
                                 attachment=attachment_identity(file_msg), metadata={
                                     "expiration_date": expiration_ts, "builder_id": str(ctx.author.id),
                                 })
        else:
            new_base = BaseLayout(
                district=district,
                link=base_link,
                image_url=file_msg.attachments[0].url if file_msg else UNAVAILABLE_IMAGE,
                records=[],
                expiration_date=expiration_ts,
                builder_id=str(ctx.author.id),
                quality="Private",
                image_attachment=attachment_identity(file_msg) if file_msg else None,
                fingerprint_extraction=ExtractionState(
                    source_revision=0, pipeline_id=PIPELINE_ID,
                ) if file_msg else None,
            )
            await new_base.insert()

        image_status = "Fingerprint extraction pending." if file_msg else "Screenshot unavailable."
        await ctx.send(f"{SUCCESS_EMOJI} You have successfully added a private base for `{district}`. "
                       f"{image_status}", ephemeral=True)

    @private_group.subcommand(sub_cmd_name="list", sub_cmd_description="List your private bases")
    @ipy.slash_option(
        name="district",
        description="Filter by district",
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
    async def base_private_list(self, ctx: ipy.SlashContext, district: str = "Any", embed_type: str = "Performance"):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered. Please register first.", ephemeral=True)
            return

        query = BaseLayout.find(BaseLayout.quality == "Private", BaseLayout.builder_id == str(ctx.author.id))

        if district != "Any":
            query = query.find(BaseLayout.district == district)

        bases = await query.sort("_id").to_list()

        if not bases:
            msg = f"{ERROR_EMOJI} You have no private bases"
            if district != "Any":
                msg += f" for `{district}`"
            msg += "."
            await ctx.send(msg, ephemeral=True)
            return

        # pagination and buttons
        index = 0
        base = bases[index]
        
        is_preset = False
        if base.district in player.base_presets:
            preset_base = player.base_presets[base.district]
            is_preset = preset_base and preset_base.link == base.link

        left_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="◀️️",
            custom_id=f"p_base_left|{district}|{index - 1}",
            disabled=True,
        )

        right_button = ipy.Button(
            style=ipy.ButtonStyle.PRIMARY,
            label="▶️",
            custom_id=f"p_base_right|{district}|{index + 1}",
            disabled=(len(bases) - 1 <= index)
        )
        
        preset_label = "Preset Remove" if is_preset else "Preset Add"
        preset_style = ipy.ButtonStyle.DANGER if is_preset else ipy.ButtonStyle.SUCCESS
        preset_action = "remove" if is_preset else "add"
        preset_button = ipy.Button(
            style=preset_style,
            label=preset_label,
            custom_id=f"p_base_preset|{district}|{index}|{base.id}|{preset_action}"
        )

        remove_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Delete",
            custom_id=f"p_base_delete|{district}|{index}|{base.id}"
        )

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))
        await ctx.send(
            embed=embed, ephemeral=True, 
            components=ipy.ActionRow(
                left_button, right_button, preset_button, remove_button
            )
        )

    @ipy.component_callback(re.compile(r"^p_base_(left|right)\|.*\|\d+$"))
    async def p_base_pagination(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(edit_origin=True)
        action, district, index_str = ctx.custom_id.split("|")
        action = action.split("_")[1]
        index = int(index_str)
        
        if action == "left":
            index -= 1
        elif action == "right":
            index += 1
            
        await update_private_base_message(ctx, district, index)

    @ipy.component_callback(re.compile(r"^p_base_preset\|[^|]+\|\d+(?:\|[0-9a-f]{24}(?:\|(?:add|remove))?)?$"))
    async def p_base_preset(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        _, district, index_str, *identity = ctx.custom_id.split("|")
        index = int(index_str)

        base = await find_private_base_action(ctx, district, identity[0] if identity else None)
        if base is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Base not found.", ephemeral=True)
            return
        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        
        if not player:
            # Should not happen if they are adding private bases, but good to check
            await ctx.send(f"{ERROR_EMOJI} Player not found.", ephemeral=True)
            return

        remove_requested = identity[1] == "remove" if len(identity) > 1 else None
        if remove_requested is None:
            for row in ctx.message.components:
                for component in row.components:
                    if component.custom_id == ctx.custom_id and component.label in {"Preset Add", "Preset Remove"}:
                        remove_requested = component.label == "Preset Remove"
        if remove_requested is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Reopen your private bases to use this preset control.", ephemeral=True)
            return
        if remove_requested:
            await clear_private_base_preset(player, base)
            action_str = "removed from"
        else:
            await player.set({f"base_presets.{base.district}": base})
            action_str = "added to"
        await ctx.send(f"{SUCCESS_EMOJI} Base {action_str} presets.", ephemeral=True)
        await update_private_base_message(ctx, district, index)

    @ipy.component_callback(re.compile(r"^p_base_delete\|[^|]+\|\d+(?:\|[0-9a-f]{24})?$"))
    async def p_base_delete(self, ctx: ipy.ComponentContext):
        if ctx.author and int(ctx.author.id) != int(ctx.message.interaction.user):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        _, district, index_str, *identity = ctx.custom_id.split("|")
        index = int(index_str)

        base = await find_private_base_action(ctx, district, identity[0] if identity else None)
        if base is None:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(f"{ERROR_EMOJI} Base not found.", ephemeral=True)
            return
        
        player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
        if player:
            await clear_private_base_preset(player, base)

        deleted = await base.get_pymongo_collection().delete_one({
            "_id": base.id, "quality": "Private", "builder_id": str(ctx.author.id), "district": base.district,
        })
        if deleted.deleted_count == 0:
            await ctx.edit(ctx.message, components=[])
            await ctx.send(
                f"{ERROR_EMOJI} This base changed or is no longer available to delete. Reopen your private bases.",
                ephemeral=True,
            )
            return
        await ctx.send(f"{SUCCESS_EMOJI} Base deleted.", ephemeral=True)
        
        # update message, index might need adjustment if it was the last element
        await update_private_base_message(ctx, district, index)

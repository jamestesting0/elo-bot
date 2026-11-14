import difflib
import os
import secrets
import tempfile
from datetime import datetime

import anyio
import interactions as ipy
import validators
from interactions import utils

import elo_bot.extensions.base.groups as groups
import elo_bot.features.hosting.war.helper_preserve as helper_preserve
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.game import DISTRICT_CODES_REVERSED
from elo_bot.config.ids import (
    BASE_RENEWAL_LOGS_CHANNEL,
    BASE_RENEWAL_THREAD,
    LVL1_ROLES,
    LVL2_ROLES,
    LVL3_ROLES,
    MAIN_GUILD,
    WHITELISTED_STAFFS,
)
from elo_bot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    LINE_SEPERATOR_GIF,
    RED_COLOR,
)
from elo_bot.db import BaseLayout
from elo_bot.discord.checks import has_any, has_any_id
from elo_bot.discord.members import member_guild
from elo_bot.extensions.base.groups import BASE_QUALITY_CHOICES
from elo_bot.features.bases.fingerprints.images import attachment_identity, download_base_screenshot
from elo_bot.features.bases.fingerprints.ingestion import replace_source
from elo_bot.db.fingerprint_types import PIPELINE_ID, ExtractionState
from elo_bot.features.bases.layouts.queries import BaseAutocompleteRow
from elo_bot.features.bases.layouts.ui import (
    _builder_choice_label,
    _builder_names,
    _cached_user_name,
    base_embed_names,
    construct_base_embed,
)
from elo_bot.features.bases.screenshots import capture_base_screenshot
from elo_bot.integrations.files import delete_file


class BaseManagementCommands:
    base = groups.base
    set_group = groups.set_group

    async def send_base_renewal_log(self, base: BaseLayout) -> None:
        embed = ipy.Embed(
            title=f"{base.district} Base Renewed",
            description=base.link,
            color=COLOR,
        )
        embed.set_image(base.image_url)
        channel = await self.bot.fetch_channel(BASE_RENEWAL_LOGS_CHANNEL)
        await channel.send(embed=embed)

    async def send_base_renewal_failure_log(self, base: BaseLayout, notification: str) -> None:
        embed = ipy.Embed(
            title=f"{base.district} Base Renewal Failed",
            description=f"{base.link}\n\n{notification}",
            color=RED_COLOR,
        )
        embed.set_image(base.image_url)
        channel = await self.bot.fetch_channel(BASE_RENEWAL_LOGS_CHANNEL)
        await channel.send(embed=embed)

    @base.subcommand(sub_cmd_name="info", sub_cmd_description="Show detailed information of a base")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        autocomplete=True,
        required=True
    )
    @ipy.slash_option(
        name="embed_type",
        description="The type of embed to display",
        choices=[
            ipy.SlashCommandChoice(name="Performance", value="Performance"),
            ipy.SlashCommandChoice(name="Defense Log", value="Defense Log")
        ],
        opt_type=ipy.OptionType.STRING,
    )
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def base_info(self, ctx: ipy.SlashContext, base_link: str, embed_type: str = "Performance",
                        hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        if base_link.isdigit():
            try:
                index = int(base_link)
                bases = await BaseLayout.find_all().sort("_id").to_list()
                base = bases[index]
            except (ValueError, IndexError):
                await ctx.send(f"{ERROR_EMOJI} The provided base link is invalid.", ephemeral=hidden)
                return
        else:
            base = await BaseLayout.find_one(BaseLayout.link == base_link)
            if not base:
                await ctx.send(f"{ERROR_EMOJI} The base is not part of the helper's bases.",
                               ephemeral=hidden)

                return

        if base.builder_id != str(ctx.author.id) and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You may not access bases built by others.",
                           ephemeral=hidden)

            return

        guild = await member_guild(ctx)
        embed = construct_base_embed(base, embed_type, names=await base_embed_names(guild, base, embed_type))

        await ctx.send(embed=embed, ephemeral=hidden)

    @base_info.autocomplete(option_name="base_link")
    async def base_autocomplete(self, ctx: ipy.AutocompleteContext):
        bases = await BaseLayout.find_all().sort("_id").project(BaseAutocompleteRow).to_list()
        author_id = str(ctx.author.id)
        is_owner = author_id == str(self.bot.owner.id)

        choices = {}
        for index, base in enumerate(bases, start=1):
            if not is_owner and base.builder_id != author_id:
                continue
            builder = _builder_choice_label(self.bot, ctx.guild, base.builder_id)
            choices[f"{base.district} {builder} {index}".lower()] = {
                "name": f"{base.district} by {builder} ({index})"[:100],
                "value": index - 1,
            }

        if not choices:
            return await ctx.send([])

        user_input = ctx.input_text or ""
        if not user_input:
            return await ctx.send(list(choices.values())[:25])

        returns = difflib.get_close_matches(
            user_input.lower(),
            choices.keys(),
            n=25,
            cutoff=0.15,
        )
        await ctx.send([choices[item] for item in returns])

    @base.subcommand(sub_cmd_name="add", sub_cmd_description="Add a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_quality",
        description="The quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    @ipy.slash_option(
        name="builder",
        description="Builder of the base",
        opt_type=ipy.OptionType.USER
    )
    async def base_add(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment | None = None,
                       base_quality: str = "Average",
                       builder: ipy.User = None):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or
                (len(base_link.split("%")) > 1 and base_link.split("%")[1] != "3ACC")):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)
            return

        # handle image upload
        token = secrets.token_hex(8)
        temp_path = f"temp/{token}.webp"

        downloaded = False
        try:
            if base_ss is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, base_ss.url, temp_path)
            else:
                downloaded = await capture_base_screenshot(base_link, str(ctx.author.id), temp_path, ctx=ctx)
                if not downloaded:
                    await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. No base was changed.",
                                   ephemeral=True)
            if not downloaded:
                return
            file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
        except ipy.errors.HTTPException:
            if base_ss is not None:
                raise
            await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. No base was changed.", ephemeral=True)
            return
        finally:
            if downloaded or os.path.exists(temp_path):
                await delete_file(temp_path)
        if not file_msg.attachments:
            await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
            return

        # update database
        expiration_ts = datetime.now().timestamp() + 15 * 86400
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        existing_base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if existing_base:
            await replace_source(existing_base, file_msg.attachments[0].url, district,
                                 attachment=attachment_identity(file_msg), metadata={
                                     "expiration_date": expiration_ts, "builder_id": str(builder.id) if builder else None,
                                     "quality": base_quality,
                                 })
        else:
            new_base = BaseLayout(
                district=district,
                link=base_link,
                image_url=file_msg.attachments[0].url,
                records=[],
                expiration_date=expiration_ts,
                builder_id=str(builder.id) if builder else None,
                quality=base_quality,
                image_attachment=attachment_identity(file_msg),
                fingerprint_extraction=ExtractionState(source_revision=0, pipeline_id=PIPELINE_ID),
            )
            await new_base.insert()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully added a base for `{district}`. Fingerprint extraction pending.",
            ephemeral=True,
        )

    @base.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_remove(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        await base.delete()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully removed a base for `{district}`.")

    @base.subcommand(sub_cmd_name="renew", sub_cmd_description="Preserve a base link and renew its expiration")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="base_link",
        description="The stored base link",
        opt_type=ipy.OptionType.STRING,
        required=True,
    )
    async def base_renew(self, ctx: ipy.SlashContext, base_link: str) -> None:
        await helper_preserve.autorenew_base(self, ctx, base_link)

    @set_group.subcommand(sub_cmd_name="link", sub_cmd_description="Set a renewed link for a base")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="old_base_link",
        description="The old base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="new_base_link",
        description="The new base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_set_link(self, ctx: ipy.SlashContext, old_base_link: str, new_base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(new_base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not new_base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in new_base_link.split("=") or new_base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == old_base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        base.link = new_base_link
        base.expiration_date = datetime.now().timestamp() + 15 * 86400
        await base.save_changes()
        await self.send_base_renewal_log(base)

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully renewed a base for `{base.district}`.")

    @set_group.subcommand(sub_cmd_name="expired", sub_cmd_description="Set a base as expired")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def base_set_expired(self, ctx: ipy.SlashContext, base_link: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")

            return

        base.expiration_date = datetime.now().timestamp()
        await base.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} This is base is now marked as expired.")

    @set_group.subcommand(sub_cmd_name="quality", sub_cmd_description="Set the base builder for a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_quality",
        description="Quality of the base",
        opt_type=ipy.OptionType.STRING,
        choices=BASE_QUALITY_CHOICES,
        required=True
    )
    async def base_set_quality(self, ctx: ipy.SlashContext, base_link: str, base_quality: str):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")
            return

        base.quality = base_quality
        await base.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set the quality of the base as **{base_quality}**.")

    @set_group.subcommand(sub_cmd_name="builder", sub_cmd_description="Set the base builder for a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="builder",
        description="Builder of the base",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    async def base_set_builder(self, ctx: ipy.SlashContext, base_link: str, builder: ipy.User):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.")
            return

        await base.set({"builder_id": str(builder.id) if not builder.bot else None})

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set {builder.mention} as the builder.")

    @set_group.subcommand(sub_cmd_name="image", sub_cmd_description="Set the image of a base")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="image_link",
        description="The image link of the base",
        opt_type=ipy.OptionType.STRING,
        required=False
    )
    async def base_set_image(self, ctx: ipy.SlashContext, base_link: str, image_link: str | None = None):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == base_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} This base is not stored in the helper's base bank.", ephemeral=True)
            return

        if image_link is not None and "ephemeral-attachments" in image_link.split("/"):
            await ctx.send(f"{ERROR_EMOJI} The attachment link is marked as ephemeral.", ephemeral=True)
            return

        with tempfile.TemporaryDirectory(prefix="base-screenshot-") as directory:
            temp_path = os.path.join(directory, "image")
            if image_link is not None:
                downloaded = await download_base_screenshot(self.bot, ctx, image_link, temp_path)
            else:
                downloaded = await capture_base_screenshot(
                    base_link, str(ctx.author.id), temp_path, ctx=ctx, keep_secret=base.quality == "Private",
                )
                if not downloaded:
                    await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. Your previous image was kept.",
                                   ephemeral=True)
            if not downloaded:
                return
            try:
                file_msg = await self.bot.owner.send(file=ipy.File(temp_path), silent=True)
            except ipy.errors.HTTPException:
                if image_link is not None:
                    raise
                await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. Your previous image was kept.",
                               ephemeral=True)
                return
            if not file_msg.attachments:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when uploading the image.", ephemeral=True)
                return

        await replace_source(base, file_msg.attachments[0].url, base.district,
                             attachment=attachment_identity(file_msg))

        await ctx.send(f"{SUCCESS_EMOJI} Screenshot saved. Fingerprint extraction pending.", ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(hours=24))
    async def base_expiration_warning(self):
        now = int(datetime.now().timestamp())
        warning_threshold = now + (3 * 86400)
        expiring_bases = await BaseLayout.find(
            BaseLayout.expiration_date <= warning_threshold
        ).to_list()

        warning_thread = await self.bot.fetch_channel(BASE_RENEWAL_THREAD)
        warned = False
        for base in expiring_bases:
            renew_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Renew",
                custom_id="renew_base_button",
                emoji=ipy.PartialEmoji(name="🔄")
            )

            ts = ipy.Timestamp.fromtimestamp(base.expiration_date).format(ipy.TimestampStyles.RelativeTime)
            embed = ipy.Embed(
                title=f"**{base.district} Base Expiring Soon**",
                description=f"Expired {ts}" if base.expiration_date < now else f"Expires {ts}",
                fields=[
                    ipy.EmbedField(
                        name="**Old Base Link**",
                        value=base.link
                    )
                ],
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=COLOR
            )
            embed.set_image(base.image_url)

            await warning_thread.send(embed=embed, components=renew_button)
            warned = True
            await helper_preserve.automatically_renew_base(self, base, str(self.bot.user.id))

        if warned:
            await warning_thread.send(LINE_SEPERATOR_GIF)

    @ipy.component_callback("renew_base_button")
    @ipy.check(has_any(ipy.has_any_role(*LVL1_ROLES), has_any_id(*WHITELISTED_STAFFS)))
    async def base_renew_button(self, ctx: ipy.ComponentContext):
        old_link = ctx.message.embeds[0].fields[0].value
        existing_base = await BaseLayout.find_one(BaseLayout.link == old_link)
        if not existing_base:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that the base is already renewed.", ephemeral=True)
            return

        modal = ipy.Modal(
            ipy.ParagraphText(
                label="Insert the renewed base link here",
                max_length=200,
                placeholder="Please make sure that the renewed link is correct!",
                custom_id="renewed_link",
                required=False
            ),
            title="Base Renew Modal",
            custom_id="renew_base_modal",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback("renew_base_modal")
    async def base_renew_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        old_link = ctx.message.embeds[0].fields[0].value
        new_link = response["renewed_link"]

        if (not new_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in new_link.split("=") or new_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        old_district = DISTRICT_CODES_REVERSED[old_link.split('%')[2]]
        new_district = DISTRICT_CODES_REVERSED[new_link.split('%')[2]]
        if old_district != new_district:
            await ctx.send(f"{ERROR_EMOJI} Your district of your renewed link is incorrect. The district of the link "
                           f"you provided is `{new_district}`, but the expected district is `{old_district}`.",
                           ephemeral=True)

            return

        if new_link and old_link == new_link:
            await ctx.send(f"{ERROR_EMOJI} The new link you have entered is the same as the old link.",
                           ephemeral=True)

            return

        base = await BaseLayout.find_one(BaseLayout.link == old_link)
        if not base:
            await ctx.send(f"{ERROR_EMOJI} The original base could not be found (it may have been deleted).",
                           ephemeral=True)
            return

        if new_link:
            base.link = new_link
        base.expiration_date = datetime.now().timestamp() + 15 * 86400

        await base.save_changes()
        await self.send_base_renewal_log(base)

        ctx.message.embeds[0].add_field(name="New Base Link", value=new_link)
        await ctx.message.edit(embeds=ctx.message.embeds,
                               components=utils.misc_utils.disable_components(*ctx.message.components))

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully renewed a base for `{new_district}`.",
                       ephemeral=True)

    async def warm_builder_names(self) -> None:
        names = _builder_names(self.bot)
        rows = await BaseLayout.find_all().project(BaseAutocompleteRow).to_list()
        guild = self.bot.get_guild(MAIN_GUILD)
        for row in rows:
            builder_id = str(row.builder_id) if row.builder_id else None
            if not builder_id or builder_id in names:
                continue
            cached = guild.get_member(builder_id) if guild is not None else None
            if cached is None:
                cached = self.bot.get_user(builder_id)
            name = _cached_user_name(cached)
            if name:
                names[builder_id] = name
                continue
            user = await self.bot.fetch_user(builder_id)
            name = _cached_user_name(user)
            if name:
                names[builder_id] = name
            await anyio.sleep(0.1)

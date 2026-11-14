import os
import secrets
from datetime import datetime

import validators

from elobot.features.bases.screenshots import capture_base_screenshot
import interactions as ipy
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, UNAVAILABLE_IMAGE
from elobot.config.game import DISTRICT_CHOICES, DISTRICT_CODES_REVERSED
from elobot.config.emojis import DISTRICT_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import GUILD_IDS
from elobot.db import BaseLayout, Player
from elobot.features.bases.fingerprints.images import attachment_identity, download_base_screenshot
from elobot.features.bases.fingerprints.ingestion import replace_source
from elobot.db.fingerprint_types import ExtractionState, PIPELINE_ID
from elobot.integrations.files import delete_file


class PresetCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="preset", description="", scopes=GUILD_IDS)

    base_group = base.group(name="base", description="")

    @base_group.subcommand(sub_cmd_name="view", sub_cmd_description="View your base presets")
    async def preset_base_view(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)

            return

        base_presets = [
            (district, base_link)
            for district, base_link in player.base_presets.items()
            if base_link is not None
        ]
        if not base_presets:
            await ctx.send(
                f"{ERROR_EMOJI} You have not preset any bases. Feel free to add base presets with this command "
                f"{self.bot.mention_command('preset base add', scope=int(ctx.guild.id))}!", ephemeral=True)

            return

        embeds = []
        for district, base_link in base_presets:
            expiration_date = base_link.expiration_date
            if expiration_date is None:
                expiration_date = datetime.now().timestamp() - 90 * 86400
            expiration_ts = ipy.Timestamp.fromtimestamp(expiration_date)
            expiration_ts = expiration_ts.format(style=ipy.TimestampStyles.RelativeTime)

            embed = ipy.Embed(
                title=f"**{district}**",
                description=f"{base_link.link}\n"
                            f"*Expires {expiration_ts}*",
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=COLOR
            )
            embed.set_image(base_link.image_url)
            embeds.append(embed)

        await ctx.send(f"{SUCCESS_EMOJI} here are your base presets!", embeds=embeds, ephemeral=True)

    @base_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove a base preset")
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    async def preset_base_remove(self, ctx: ipy.SlashContext, district: str):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)

            return

        if district in player.base_presets:
            player.base_presets[district] = None
            await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully removed your base preset for **{district}**.",
                       ephemeral=True)

    @base_group.subcommand(sub_cmd_name="remove_all", sub_cmd_description="Remove all base presets")
    async def preset_base_remove_all(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)

            return

        if player.base_presets:
            await player.set({"base_presets": {}})

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully removed **all** your base presets.",
                       ephemeral=True)

    @base_group.subcommand(sub_cmd_name="add", sub_cmd_description="Add a base preset")
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
    async def preset_base_add(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment | None = None):
        await ctx.defer(ephemeral=True)

        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)

            return

        if (not base_link.startswith("https://link.clashofclans.com") or
                "OpenLayout&id" not in base_link.split("=") or base_link.split("%")[1] != "3ACC"):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided does not look like a Clash of "
                           f"Clans base link.", ephemeral=True)

            return

        is_helper_base = await BaseLayout.find_one(
            BaseLayout.link == base_link,
            BaseLayout.quality != "Private"
        )
        if is_helper_base:
            await ctx.send(f"{ERROR_EMOJI} The link is a helper's base. You may still use this base, but please "
                           f"generate a different link for it.",
                           ephemeral=True)

            return

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running the "
                           f"command `/player register`!", ephemeral=True)

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
            await ctx.send(f"{ERROR_EMOJI} The screenshot could not be uploaded. Your previous preset was kept.",
                           ephemeral=True)
            return
        finally:
            if downloaded or os.path.exists(temp_path):
                await delete_file(temp_path)

        base_image_url = file_msg.attachments[0].url if file_msg else UNAVAILABLE_IMAGE

        expiration_ts = datetime.now().timestamp() + 15 * 86400
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        existing_base = await BaseLayout.find_one(
            BaseLayout.link == base_link,
            BaseLayout.quality == "Private"
        )
        if existing_base:
            if file_msg is None:
                await ctx.send(f"{ERROR_EMOJI} Screenshot unavailable. Your previous preset was kept.",
                               ephemeral=True)
                return
            await replace_source(existing_base, base_image_url, district,
                                 attachment=attachment_identity(file_msg), metadata={
                                     "expiration_date": expiration_ts, "builder_id": str(ctx.author.id),
                                 })

            player.base_presets[district] = existing_base
        else:
            new_base = BaseLayout(
                district=district,
                link=base_link,
                image_url=base_image_url,
                records=[],
                expiration_date=expiration_ts,
                quality="Private",
                builder_id=str(ctx.author.id),
                image_attachment=attachment_identity(file_msg) if file_msg else None,
                fingerprint_extraction=ExtractionState(
                    source_revision=0, pipeline_id=PIPELINE_ID,
                ) if file_msg else None,
            )
            await new_base.insert()
            
            player.base_presets[district] = new_base

        await player.save_changes()

        image_status = "Fingerprint extraction pending." if file_msg else "Screenshot unavailable."
        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set your base for **{district}**. "
                       f"{image_status} "
                       f"To remove base presets, please run "
                       f"{self.bot.mention_command('preset base remove', int(ctx.guild.id))}.",
                       ephemeral=True)

    district_group = base.group(name="district", description="")

    @district_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove district presets")
    async def preset_district_remove(self, ctx: ipy.SlashContext):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)
            
            return

        player.district_presets = [None, None, None]
        player.secondary_district_presets = [None, None, None]
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully removed all your district presets.",
                       ephemeral=True)

    @district_group.subcommand(sub_cmd_name="add", sub_cmd_description="Preset district for future matches")
    @ipy.slash_option(
        name="district1",
        description="The 1st district you want to attack",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
            ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
            ipy.SlashCommandChoice(name="Wizard Valley", value="Wizard Valley"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="district2",
        description="The 2nd district you want to attack",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Balloon Lagoon", value="Balloon Lagoon"),
            ipy.SlashCommandChoice(name="Builder's Workshop", value="Builder's Workshop"),
            ipy.SlashCommandChoice(name="Golem Quarry", value="Golem Quarry")
        ],
        required=True
    )
    @ipy.slash_option(
        name="district3",
        description="The 3rd district you want to attack",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines"),
            ipy.SlashCommandChoice(name="Capital Peak", value="Capital Peak"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="secondary_district1",
        description="The fallback if Wizard Valley is unavailable",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
            ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
        ],
        required=False
    )
    @ipy.slash_option(
        name="secondary_district_3",
        description="The fallback if Capital Peak is unavailable",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines"),
        ],
        required=False
    )
    async def preset_district_add(
            self,
            ctx: ipy.SlashContext,
            district1: str,
            district2: str,
            district3: str,
            secondary_district1: str | None = None,
            secondary_district_3: str | None = None,
    ):
        if [district1, district2, district3] == [None, None, None]:
            await ctx.send(f"{ERROR_EMOJI} Please set at least one district!", ephemeral=True)

            return

        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)
            
            return

        player.district_presets = [district1, district2, district3]
        player.secondary_district_presets = [secondary_district1, None, secondary_district_3]
        await player.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully set the districts you would like to choose "
                       f"by default. To remove district presets, please run "
                       f"{self.bot.mention_command('preset district remove', int(ctx.guild.id))},",
                       ephemeral=True)

    @district_group.subcommand(sub_cmd_name="view", sub_cmd_description="View your district presets")
    async def preset_district_view(self, ctx: ipy.SlashContext):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))

        if not player:
            await ctx.send(
                f"{ERROR_EMOJI} You are not registered in the elo system. Please register by running this command "
                f"{self.bot.mention_command('player register')}!", ephemeral=True)

            return

        # filter out None values to check if empty
        if not player.district_presets or all(d is None for d in player.district_presets):
            await ctx.send(
                f"{ERROR_EMOJI} You have not preset any districts yet. Feel free to add district presets "
                f"with this command "
                f"{self.bot.mention_command('preset district add', scope=int(ctx.guild.id))}!", ephemeral=True)

            return

        embed = ipy.Embed(
            title="**District Presets**",
            description="",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )
        
        presets = player.district_presets
            
        for i in range(3):
            if presets[i]:
                embed.description += f"{i+1}. {DISTRICT_EMOJI} {presets[i]}\n"
            else:
                embed.description += f"{i + 1}. {ERROR_EMOJI} Not set\n"

        await ctx.send(embed=embed, ephemeral=True)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Preset commands loaded")

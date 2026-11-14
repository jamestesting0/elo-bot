import asyncio
import re
import sys
import traceback
from datetime import datetime

import coc
import interactions as ipy

from elo_bot.discord import runtime
from elo_bot.app.events import on_raid_weekend_end
from elo_bot.config.emojis import ERROR_EMOJI, STAFF_EMOJI, SUCCESS_EMOJI
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE
from elo_bot.config.settings import COC_API_EMAIL, COC_API_KEYS, COC_API_PASSWORD


async def report_error(ctx: ipy.SlashContext | ipy.ModalContext | ipy.ComponentContext, error):
    full_error = traceback.format_exception(type(error), error, error.__traceback__)
    full_error = "".join(str(i) for i in full_error)
    print(full_error, file=sys.stderr)

    report_button = ipy.Button(
        style=ipy.ButtonStyle.DANGER,
        label="Report Bug",
        custom_id="report_bug_button",
        emoji=ipy.PartialEmoji(name="📢")
    )

    await ctx.send(f"{ERROR_EMOJI} An unexpected error has occurred. Please try again!\n\n"
                   f"*If you believe this is a bug, please press the `Report Bug` button! Thanks!*",
                   components=report_button, ephemeral=True)

    try:
        res: ipy.ComponentContext = (await runtime.bot.wait_for_component(components=report_button, timeout=180)).ctx
    except asyncio.TimeoutError:
        return

    await res.defer(ephemeral=True)

    if isinstance(ctx, ipy.ComponentContext):
        error_info = f"Component: `{ctx.custom_id}`"
    elif isinstance(ctx, ipy.ModalContext):
        error_info = f"Modal: `{ctx.custom_id}`"
    else:
        command_name = str(ctx.command.name)
        command_name += f" {str(ctx.command.group_name)}" if ctx.command.group_name else ""
        command_name += f" {str(ctx.command.sub_cmd_name)}" if ctx.command.sub_cmd_name else ""
        error_info = f"Command: `{command_name}`"

    embed = ipy.Embed(
        title=f"**{str(error)[:252]}**",
        description=f"```\n{full_error[-(4096 - 8):]}\n```",
        fields=[
            ipy.EmbedField(
                name="**Basic Information**",
                value=f"Reported by: {ctx.author.mention} `{ctx.author}`\n"
                      f"{error_info}\n",
                inline=False
            )
        ],
        footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
        color=COLOR
    )

    for key, value in ctx.kwargs.items():
        if isinstance(value, (ipy.User, ipy.Member, ipy.GuildChannel, ipy.Role)):
            ctx.kwargs[key] = value.id
        if isinstance(value, ipy.Attachment):
            ctx.kwargs[key] = value.url

    if ctx.kwargs:
        embed.add_field(name="**Context**", value=f"```json\n{ctx.kwargs}\n```")

    button = ipy.Button(
        style=ipy.ButtonStyle.SECONDARY,
        label="Respond",
        custom_id=f"bug_respond_button|{ctx.author.id}",
        emoji=STAFF_EMOJI
    )

    await runtime.bot.owner.send(embed=embed, components=button)

    await res.send(f"{SUCCESS_EMOJI} Thanks for reporting the bug! A message has been sent "
                   f"to the bot developer, there will be a fix **ASAP**!",
                   ephemeral=True)


@ipy.listen(ipy.events.Error, disable_default_listeners=True)
async def on_error(event: ipy.events.Error):
    error = event.error
    if "Unknown interaction" in str(error):
        print(str(error), file=sys.stderr)
    else:
        full_error = traceback.format_exception(type(error), error, error.__traceback__)
        full_error = "".join(str(i) for i in full_error)
        print(full_error, file=sys.stderr)

        embed = ipy.Embed(
            title=f"**{str(error)[:252]}**",
            description=f"```\n{full_error[-(4096 - 8):]}\n```",
            footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await runtime.bot.owner.send(embed=embed, silent=True)


@ipy.listen(ipy.events.AutocompleteError, disable_default_listeners=True)
async def on_autocomplete_error(event: ipy.events.AutocompleteError):
    ctx: ipy.AutocompleteContext = event.ctx
    error = event.error

    if isinstance(error, ipy.errors.HTTPException) and error.code in (10062, 40060):
        print(str(error), file=sys.stderr)
        return

    notify_owner = False
    if isinstance(error, coc.errors.GatewayError):
        choices = [{
            "name": "Clash of Clans' API server is having some unexpected issues...",
            "value": 0
        }]
    elif isinstance(error, coc.errors.Maintenance):
        choices = [{
            "name": "Clash of Clans is currently under maintenance...",
            "value": 0
        }]
    else:
        choices = [{
            "name": "Unexpected error, please report it to @jamestesting!",
            "value": 0
        }]
        notify_owner = True

    try:
        await ctx.send(choices)
    except ipy.errors.HTTPException as response_error:
        if response_error.code not in (10062, 40060):
            raise
        print(str(response_error), file=sys.stderr)

    if not notify_owner:
        return

    full_error = traceback.format_exception(type(error), error, error.__traceback__)
    full_error = "".join(str(i) for i in full_error)
    print(full_error, file=sys.stderr)

    embed = ipy.Embed(
        title=f"**{str(error)[:252]}**",
        description=f"```\n{full_error[-(4096 - 8):]}\n```",
        footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
        color=COLOR
    )

    await runtime.bot.owner.send(embed=embed, silent=True)


@ipy.listen(ipy.events.CommandError, disable_default_listeners=True)
async def on_command_error(event: ipy.events.CommandError):
    ctx: ipy.SlashContext = event.ctx
    error = event.error

    if isinstance(error, ipy.errors.MaxConcurrencyReached):
        bucket = error.max_conc.bucket
        bucket_name = "user" if bucket is ipy.Buckets.DEFAULT else bucket.name.lower()
        await ctx.send(
            f"{ERROR_EMOJI} This command can only be used {error.max_conc.concurrent} time(s) at once "
            f"per {bucket_name}.",
            ephemeral=True,
        )
        return

    if isinstance(error, ipy.errors.CommandCheckFailure):
        await ctx.send(f"{ERROR_EMOJI} You do not have permission to run this command!",
                       ephemeral=True)
    elif isinstance(error, ipy.errors.CommandOnCooldown):
        cooldown_time = error.cooldown.get_cooldown_time()
        now = datetime.now().timestamp()
        relative_ts = ipy.Timestamp.fromtimestamp(now + cooldown_time).format(ipy.TimestampStyles.RelativeTime)
        await ctx.send(f"{ERROR_EMOJI} Command is on cooldown, you can use it again "
                       f"{relative_ts}.", ephemeral=True)
    elif isinstance(error, coc.errors.GatewayError):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans' API server is having some unexpected issues.",
                       ephemeral=True)
    elif isinstance(error, coc.errors.Maintenance):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans is currently under maintenance.",
                       ephemeral=True)
    elif isinstance(error, coc.errors.Forbidden):
        await ctx.send(f"{ERROR_EMOJI} The bot failed to authorize to the Clash of Clans API. Please try again "
                       f"in some seconds, attempting to fix.",
                       ephemeral=True)

        if runtime.bot.coc_client_lock.locked():
            return

        async with runtime.bot.coc_client_lock:
            await runtime.bot.coc_client.close()
            coc_client = coc.EventsClient(key_names=COC_API_KEYS[runtime.VERSION])
            coc_client.add_events(on_raid_weekend_end)
            await coc_client.login(COC_API_EMAIL, COC_API_PASSWORD)
            runtime.bot.coc_client = coc_client
    elif "Unknown interaction" in str(error):
        await ctx.send(f"{ERROR_EMOJI} Looks like something is wrong with Discord, please try again later.",
                       ephemeral=True)
    else:
        await report_error(ctx, error)


@ipy.listen(ipy.events.ComponentError, disable_default_listeners=True)
async def on_component_error(event: ipy.events.ComponentError):
    ctx: ipy.ComponentContext = event.ctx
    error = event.error

    if isinstance(error, ipy.errors.MaxConcurrencyReached):
        bucket = error.max_conc.bucket
        bucket_name = "user" if bucket is ipy.Buckets.DEFAULT else bucket.name.lower()
        await ctx.send(
            f"{ERROR_EMOJI} This command can only be used {error.max_conc.concurrent} time(s) at once "
            f"per {bucket_name}.",
            ephemeral=True,
        )
        return

    if isinstance(error, ipy.errors.CommandCheckFailure):
        await ctx.send(f"{ERROR_EMOJI} You do not have permission to interact with this component!",
                       ephemeral=True)
    elif isinstance(error, coc.errors.GatewayError):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans' API server is having some unexpected issues.",
                       ephemeral=True)
    elif isinstance(error, coc.errors.Maintenance):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans is currently under maintenance.",
                       ephemeral=True)
    elif "Unknown interaction" in str(error):
        await ctx.send(f"{ERROR_EMOJI} Looks like something is wrong with Discord, please try again later.",
                       ephemeral=True)
    else:
        await report_error(ctx, error)


@ipy.listen(ipy.events.ModalError, disable_default_listeners=True)
async def on_modal_error(event: ipy.events.ModalError):
    ctx: ipy.ModalContext = event.ctx
    error = event.error

    if isinstance(error, coc.errors.GatewayError):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans' API server is having some unexpected issues.",
                       ephemeral=True)
    elif isinstance(error, coc.errors.Maintenance):
        await ctx.send(f"{ERROR_EMOJI} Clash of Clans is currently under maintenance.",
                       ephemeral=True)
    elif "Unknown interaction" in str(error):
        await ctx.send(f"{ERROR_EMOJI} Looks like something is wrong with Discord, please try again later.",
                       ephemeral=True)
    else:
        await report_error(ctx, error)


@ipy.component_callback(re.compile(r"^bug_respond_button\|\d+$"))
async def bug_respond_button(ctx: ipy.ComponentContext):
    _, user_id = ctx.custom_id.split("|")

    modal = ipy.Modal(
        ipy.ParagraphText(
            label="What is your response to the bug?",
            value="",
            max_length=300,
            custom_id=user_id
        ),
        title="Bug Response Form",
        custom_id="bug_response_modal",
    )
    await ctx.send_modal(modal)


@ipy.modal_callback("bug_response_modal")
async def bug_respond_modal(ctx: ipy.ModalContext, **response):
    await ctx.defer(ephemeral=True)

    user = await runtime.bot.fetch_user(list(response.keys())[0], force=True)

    title = f"**Response from {ctx.author} `{ctx.author.id}`**"
    description = f"Dear developers, here is the response from the user who reported this bug." \
                  f"The response is:\n" \
                  f"```{list(response.values())[0]}```"

    if ctx.author in runtime.bot.owners:
        title = "**Response from the Development Team**"
        description = f"Thank you for reporting the bug to the Divide by Zero™ bot developers. " \
                      f"If you have any futher questions please use the response button!" \
                      f"Here the official response to this issue:\n" \
                      f"```{list(response.values())[0]}```"

    embed = ipy.Embed(
        title=title,
        description=description,
        fields=[
            ctx.message.embeds[0].fields[0]
        ],
        footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
        color=COLOR,
    )

    bug_respond = ipy.Button(
        style=ipy.ButtonStyle.SECONDARY,
        label="Respond",
        custom_id=f"bug_respond_button|{ctx.author.id}",
        emoji=STAFF_EMOJI
    )

    try:
        await user.send(embed=embed, components=bug_respond)
    except ipy.errors.Forbidden:
        await ctx.send(f"{ERROR_EMOJI} Discord forbidden error, the bot do not share servers with the target user!")
    else:
        ctx.message.components[0].components[0].disabled = True
        await ctx.message.edit(components=ctx.message.components)
        await ctx.send(f"{SUCCESS_EMOJI} Your response is sent to the user who reported the bug!")

import re
import asyncio
import time
from datetime import datetime, timezone
from typing import assert_never

from elo_bot.db import AuditLogEntry
import interactions as ipy
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import GUILD_IDS, LVL1_ROLES
from elo_bot.config.audit import MONITORED_COMMANDS, MONITORED_COMPONENTS, SPECIAL_MONITORED_COMMANDS
from elo_bot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.members import member_guild


_AUDITLOG_PAGE_SIZE = 7


def _auditlog_page_count(total_entries: int) -> int:
    if total_entries <= 0:
        return 0
    return (total_entries + _AUDITLOG_PAGE_SIZE - 1) // _AUDITLOG_PAGE_SIZE


def _auditlog_embed(entries: list[AuditLogEntry], names: dict[str, str]) -> ipy.Embed:
    description = ""
    for entry in entries:
        timestamp = ipy.Timestamp.fromdatetime(
            datetime.fromtimestamp(entry.recorded_at_ns / 1_000_000_000, timezone.utc)
        ).format(style=ipy.TimestampStyles.ShortDateTime)
        description += (f"{names[entry.author_id]} at {timestamp}: `{entry.command}`\n"
                        f"```json\n{entry.arguments}\n```\n\n")
    return ipy.Embed(
        title="**Auditlog**",
        description=description,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )


async def _load_auditlog_page(
        position: int, guild: ipy.Guild, *, total_pages: int | None = None,
) -> tuple[ipy.Embed | None, int]:
    if total_pages is None:
        total_pages = _auditlog_page_count(await AuditLogEntry.find_all().count())
    if position < 0 or position >= total_pages:
        return None, total_pages

    entries = await (
        AuditLogEntry.find_all()
        .sort("-recorded_at_ns")
        .skip(position * _AUDITLOG_PAGE_SIZE)
        .limit(_AUDITLOG_PAGE_SIZE)
        .to_list()
    )
    if not entries:
        return None, total_pages
    user_ids = {entry.author_id for entry in entries}
    members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
    names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
    return _auditlog_embed(entries, names), total_pages


def _build_auditlog_actionrow(position: int, total_pages: int) -> ipy.ActionRow:
    left_target = max(position - 1, 0)
    right_target = min(position + 1, total_pages - 1)
    left_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="◀️️",
        custom_id=f"auditlog_left|{left_target}",
        disabled=position == 0,
    )
    page_number_button = build_page_button(position + 1, total_pages, "auditlog_page")
    right_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="▶️",
        custom_id=f"auditlog_right|{right_target}",
        disabled=position == total_pages - 1,
    )
    return ipy.ActionRow(left_button, page_number_button, right_button)


class AuditlogCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot

    base = ipy.SlashCommand(name="auditlog", description="", scopes=GUILD_IDS)

    @ipy.listen(ipy.events.CommandCompletion)
    async def on_command_completion(self, event: ipy.events.CommandCompletion):
        ctx: ipy.SlashContext = event.ctx

        if not isinstance(ctx, ipy.SlashContext):
            return

        if not isinstance(ctx.author, ipy.Member):
            return

        command_name = f"/{ctx.command.name}"
        if ctx.command.group_name:
            command_name += f" {ctx.command.group_name}"
        if ctx.command.sub_cmd_name:
            command_name += f" {ctx.command.sub_cmd_name}"

        if not command_name.startswith("/data") and command_name not in MONITORED_COMMANDS:
            return

        arguments: dict[str, str | int | bool] = {}
        for key, value in ctx.kwargs.items():
            match value:
                case ipy.User() | ipy.Member() | ipy.GuildChannel() | ipy.Role():
                    arguments[key] = value.id
                case ipy.Attachment():
                    arguments[key] = value.url
                case bool() | int() | str():
                    arguments[key] = value
                case unreachable:
                    assert_never(unreachable)

        if command_name in SPECIAL_MONITORED_COMMANDS:
            arguments["channel_id"] = ctx.channel.id
            arguments["channel_name"] = ctx.channel.name

        await AuditLogEntry.insert(
            AuditLogEntry(
                recorded_at_ns=time.time_ns(),
                command=command_name,
                author_id=str(ctx.author.id),
                arguments=arguments,
            )
        )

    @ipy.listen(ipy.events.ComponentCompletion)
    async def on_component_completion(self, event: ipy.events.ComponentCompletion):
        ctx: ipy.ComponentContext = event.ctx

        if all(not ctx.custom_id.startswith(monitored_component) for monitored_component in MONITORED_COMPONENTS):
            return

        if not isinstance(ctx.author, ipy.Member):
            return

        await AuditLogEntry.insert(
            AuditLogEntry(
                recorded_at_ns=time.time_ns(),
                command=ctx.custom_id,
                author_id=str(ctx.author.id),
                arguments={"channel_id": ctx.channel.id, "channel_name": ctx.channel.name},
            )
        )

    @base.subcommand(sub_cmd_name="view", sub_cmd_description="View the auditlog")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def auditlog_view(self, ctx: ipy.SlashContext, hidden: bool = True):
        await ctx.defer(ephemeral=hidden)

        guild = await member_guild(ctx)
        embed, total_pages = await _load_auditlog_page(0, guild)
        if embed is None:
            await ctx.send(f"{ERROR_EMOJI} The auditlog is currently empty...", ephemeral=True)
            return

        if total_pages == 1:
            await ctx.send(embed=embed, ephemeral=hidden)
            return

        await ctx.send(
            embed=embed,
            components=[_build_auditlog_actionrow(0, total_pages)],
            ephemeral=hidden,
        )

    @ipy.component_callback(re.compile(r"^auditlog_(left|right)\|\d+$"))
    async def auditlog_arrow_button(self, ctx: ipy.ComponentContext):
        message = ctx.message
        if message is None or message.interaction is None or message.interaction.user is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        owner = getattr(message.interaction.user, "id", message.interaction.user)
        if ctx.author and str(ctx.author.id) != str(owner):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return

        _, target = ctx.custom_id.split("|")
        position = int(target)
        guild = await member_guild(ctx)
        embed, total_pages = await _load_auditlog_page(position, guild)
        if total_pages == 0:
            await ctx.send(f"{ERROR_EMOJI} The auditlog is currently empty...", ephemeral=True)
            return
        if embed is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        components = [] if total_pages == 1 else [_build_auditlog_actionrow(position, total_pages)]
        await ctx.edit_origin(embed=embed, components=components)

    @ipy.component_callback("auditlog_page")
    async def auditlog_page_button(self, ctx: ipy.ComponentContext):
        message = ctx.message
        if message is None or message.interaction is None or message.interaction.user is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        owner = getattr(message.interaction.user, "id", message.interaction.user)
        if ctx.author and str(ctx.author.id) != str(owner):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return

        await ctx.send_modal(build_page_jump_modal("auditlog_page_modal"))

    @ipy.modal_callback("auditlog_page_modal")
    async def auditlog_page_modal(self, ctx: ipy.ModalContext, **response):
        message = ctx.message
        if message is None or message.interaction is None or message.interaction.user is None:
            await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
            return

        owner = getattr(message.interaction.user, "id", message.interaction.user)
        if ctx.author and str(ctx.author.id) != str(owner):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return

        total_pages = _auditlog_page_count(await AuditLogEntry.find_all().count())
        if total_pages == 0:
            await ctx.send(f"{ERROR_EMOJI} The auditlog is currently empty...", ephemeral=True)
            return

        try:
            page = parse_page_number(response["page"], total_pages)
        except InvalidPageError as error:
            await ctx.send(str(error), ephemeral=True)
            return

        position = page - 1
        guild = await member_guild(ctx)
        embed, total_pages = await _load_auditlog_page(position, guild, total_pages=total_pages)
        if embed is None:
            await ctx.send(f"{ERROR_EMOJI} This page no longer exists.", ephemeral=True)
            return

        components = [] if total_pages == 1 else [_build_auditlog_actionrow(position, total_pages)]
        await ctx.edit(message, embed=embed, components=components)

    @base.subcommand(sub_cmd_name="clear", sub_cmd_description="Clear the auditlog")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def auditlog_clear(self, ctx: ipy.SlashContext):
        await AuditLogEntry.delete_all()

        await ctx.send(f"{SUCCESS_EMOJI} The auditlog is now cleared.", ephemeral=True)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Auditlog commands loaded")

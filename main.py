import certifi
from beanie import init_beanie
from dotenv import load_dotenv
from pymongo import AsyncMongoClient

from database_models import (
    Match,
    Clan,
    HelperTask,
    BaseLayout,
    RecordedMatch,
    Player,
    Queue,
    Season,
    EloFinal,
    Helper,
    ServerState,
    OverrideCode,
    OVERRIDE_CODE_INDEXES,
    AUDIT_LOG_INDEXES,
    AuditLogEntry,
    PendingBaseSubmission,
    GIVEAWAY_INDEXES,
    Giveaway,
    StickyMessage,
    BaseExtractionRun,
    BaseFingerprintTask,
)

load_dotenv()

import asyncio
import logging
import traceback
import sys
import re
import time
from collections import defaultdict
from datetime import datetime

import coc

from common.constants import *
from common.utils import ComponentsV2ModalContext, clear_all_base_store_slots, get_extensions
from common.base_fingerprints.service import FingerprintService
from common.finals.persistence import register_final_indexes
from database_models.final_models import FinalMatchResult, FinalPlayerProfile, FinalPrediction
from database_models.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet
from database_models.fingerprint_models import (
    BASE_FINGERPRINT_INDEXES, EXTRACTION_RUN_INDEXES, FINGERPRINT_TASK_INDEXES,
)

INTENTS = ipy.Intents.new(
    guilds=True,
    guild_emojis_and_stickers=True,
    guild_presences=True,
    guild_members=True,
    guild_moderation=True,
    guild_messages=True,
    direct_messages=True,
    message_content=True,
)

VERSION = "main" if "main" in os.path.basename(__file__) else "beta"
BETA_SYNC_FROM_MAIN = False


class DiscordApiRouteFilter(logging.Filter):
    _route_pattern = re.compile(r"^[A-Z]+::https://discord\.com/api/v\d+/.+: \d{3}$")

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._route_pattern.match(record.getMessage())


logging.getLogger("interactions").addFilter(DiscordApiRouteFilter())

bot = ipy.Client(
    token=TOKENS[VERSION],
    intents=INTENTS,
    fetch_members=VERSION == "main",
    sync_interactions=True,
    send_command_tracebacks=False,
    send_not_ready_messages=True,
    show_ratelimit_tracebacks=True,
    modal_context=ComponentsV2ModalContext,
    delete_unused_application_cmds=VERSION == "main",
    activity=ipy.Activity(
        name="DM your feedback to @jamestesting",
        type=ipy.ActivityType.PLAYING,
    ),
)


@bot.add_event_processor("raw_interaction_create")
async def dispatch_elo_interaction(event: ipy.events.RawGatewayEvent) -> None:
    data = event.data
    if str(ELO_BLACKLIST_ROLE) in data.get("member", {}).get("roles", []):
        response = {"type": 4, "data": {
            "content": f"{ERROR_EMOJI} You are banned from using Elo commands. " 
            "Please contact the staff team if you have doubts.", 
            "flags": 64,
        }}
        if data["type"] == 4:
            response = {"type": 8, "data": {"choices": []}}
        await bot.http.post_initial_response(response, token=data["token"], interaction_id=data["id"])
        return
    await ipy.Client._dispatch_interaction.callback(bot, event)


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
        res: ipy.ComponentContext = (await bot.wait_for_component(components=report_button, timeout=180)).ctx
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

    await bot.owner.send(embed=embed, components=button)

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

        await bot.owner.send(embed=embed, silent=True)


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

    await bot.owner.send(embed=embed, silent=True)


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

        if bot.coc_client_lock.locked():
            return

        async with bot.coc_client_lock:
            await bot.coc_client.close()
            coc_client = coc.EventsClient(key_names=COC_API_KEYS[VERSION])
            coc_client.add_events(on_raid_weekend_end)
            await coc_client.login(COC_API_EMAIL, COC_API_PASSWORD)
            bot.coc_client = coc_client
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

    user = await bot.fetch_user(list(response.keys())[0], force=True)

    title = f"**Response from {ctx.author} `{ctx.author.id}`**"
    description = f"Dear developers, here is the response from the user who reported this bug." \
                  f"The response is:\n" \
                  f"```{list(response.values())[0]}```"

    if ctx.author in bot.owners:
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


@ipy.slash_command(name="ban", description="Ban a member from using Elo commands.", scopes=GUILD_IDS,
                   dm_permission=False)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(name="user", description="The member to ban", opt_type=ipy.OptionType.USER, required=True)
async def ban(ctx: ipy.SlashContext, user: ipy.Member) -> None:
    await ctx.defer(ephemeral=True)
    await user.add_role(ELO_BLACKLIST_ROLE)
    await ctx.send(f"{SUCCESS_EMOJI} {user.mention} is now banned from using Elo commands.", ephemeral=True)


@ipy.slash_command(name="unban", description="Allow a banned member to use Elo commands again.", scopes=GUILD_IDS,
                   dm_permission=False)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(name="user", description="The member to unban", opt_type=ipy.OptionType.USER, required=True)
async def unban(ctx: ipy.SlashContext, user: ipy.Member) -> None:
    await ctx.defer(ephemeral=True)
    await user.remove_role(ELO_BLACKLIST_ROLE)
    await ctx.send(f"{SUCCESS_EMOJI} {user.mention} can now use Elo commands again.", ephemeral=True)


@ipy.slash_command(name="override", description="To override a restricted actions.", scopes=GUILD_IDS)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(
    name="override_type",
    description="The type of action to override",
    opt_type=ipy.OptionType.STRING,
    choices=[
        ipy.SlashCommandChoice(name="Challenge", value="challenge"),
        ipy.SlashCommandChoice(name="Match", value="match")
    ],
    required=True
)
async def override(ctx: ipy.SlashContext, override_type: str):
    await ctx.defer(ephemeral=True)

    override_code = await OverrideCode.issue(override_type)

    await ctx.send(f"{SUCCESS_EMOJI} The override code for the {override_type} is `{override_code}`.",
                   ephemeral=True)


STICKY_WEBHOOK_NAME = "Elo Sticky"
_sticky_locks: dict[str, asyncio.Lock] = {}
# Message ids this process is deleting while it resticks. The gateway reports those
# deletes too, and they must not clear the stored sticky.
_bot_sticky_deletes: set[tuple[str, str]] = set()


def sticky_channel_lock(channel_id: str) -> asyncio.Lock:
    return _sticky_locks.setdefault(channel_id, asyncio.Lock())


def note_bot_sticky_delete(channel_id: str, message_id: str) -> None:
    _bot_sticky_deletes.add((channel_id, message_id))


def take_bot_sticky_delete(channel_id: str, message_id: str) -> bool:
    key = (channel_id, message_id)
    if key not in _bot_sticky_deletes:
        return False
    _bot_sticky_deletes.remove(key)
    return True


async def sticky_webhook(channel, webhook_id: str | None):
    thread_id = None
    parent = channel
    if isinstance(channel, ipy.ThreadChannel):
        thread_id = channel.id
        parent = channel.parent_channel or await bot.fetch_channel(channel.parent_id)

    match = None
    named = None
    for hook in await parent.fetch_webhooks():
        if not isinstance(getattr(hook, "token", None), str) or not hook.token:
            continue
        if webhook_id is not None and str(hook.id) == webhook_id:
            match = hook
            break
        if hook.name == STICKY_WEBHOOK_NAME and named is None:
            named = hook
    if match is None:
        match = named or await parent.create_webhook(STICKY_WEBHOOK_NAME)
    return match, thread_id


async def send_sticky(webhook, content: str, author_name: str, avatar_url: str | None, thread_id):
    kwargs = {"username": author_name, "avatar_url": avatar_url, "wait": True}
    if thread_id is not None:
        kwargs["thread"] = thread_id
    return await webhook.send(content, **kwargs)


@ipy.slash_command(
    name="sticky",
    description="Add a sticky message at the bottom of this channel.",
    scopes=GUILD_IDS,
    dm_permission=False,
)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(
    name="message",
    description="The message to keep at the bottom of this channel",
    opt_type=ipy.OptionType.STRING,
    required=True,
    min_length=1,
    max_length=2000,
)
async def sticky(ctx: ipy.SlashContext, message: str) -> None:
    content = message.strip()
    if not content:
        await ctx.send(f"{ERROR_EMOJI} The sticky message cannot be empty.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)
    author_name = ctx.author.display_name[:80]
    avatar = ctx.author.display_avatar
    avatar_url = None if avatar is None else avatar.url
    channel_id = str(ctx.channel.id)

    async with sticky_channel_lock(channel_id):
        existing = await StickyMessage.for_channel(channel_id)
        webhook, thread_id = await sticky_webhook(
            ctx.channel, existing[0].webhook_id if existing else None,
        )
        sent = await send_sticky(webhook, content, author_name, avatar_url, thread_id)
        try:
            document = StickyMessage(
                id=str(sent.id),
                channel_id=channel_id,
                message_id=str(sent.id),
                webhook_id=str(webhook.id),
                content=content,
                author_name=author_name,
                avatar_url=avatar_url,
            )
            await StickyMessage.get_pymongo_collection().insert_one(document.model_dump(by_alias=True))
        except Exception:
            await sent.delete()
            raise

    await ctx.send(f"{SUCCESS_EMOJI} Sticky message set.", ephemeral=True)


@ipy.listen(ipy.events.MessageCreate)
async def on_sticky_message(event: ipy.events.MessageCreate) -> None:
    channel = event.message.channel
    if not isinstance(channel, (ipy.GuildText, ipy.GuildNews, ipy.ThreadChannel)):
        return

    channel_id = str(channel.id)
    if not await StickyMessage.for_channel(channel_id):
        return

    async with sticky_channel_lock(channel_id):
        stickies = await StickyMessage.for_channel(channel_id)
        if not stickies or any(
            sticky.message_id == str(event.message.id)
            or sticky.webhook_id == str(event.message.webhook_id)
            for sticky in stickies
        ):
            return

        webhook, thread_id = await sticky_webhook(channel, stickies[0].webhook_id)
        for sticky in stickies:
            old_message_id = sticky.message_id
            note_bot_sticky_delete(channel_id, old_message_id)
            try:
                await channel.delete_message(old_message_id)
            except ipy.errors.NotFound:
                take_bot_sticky_delete(channel_id, old_message_id)
                await sticky.release()
                continue
            except ipy.errors.HTTPException:
                take_bot_sticky_delete(channel_id, old_message_id)
                raise

            try:
                sent = await send_sticky(webhook, sticky.content, sticky.author_name, sticky.avatar_url, thread_id)
                try:
                    moved = await sticky.move(str(sent.id), str(webhook.id))
                except Exception:
                    await sent.delete()
                    raise
                if not moved:
                    await sent.delete()
            finally:
                take_bot_sticky_delete(channel_id, old_message_id)


async def release_deleted_sticky(channel_id: str, message_id: str) -> None:
    if take_bot_sticky_delete(channel_id, message_id):
        return
    stickies = await StickyMessage.for_channel(channel_id)
    if not any(sticky.message_id == message_id for sticky in stickies):
        return

    async with sticky_channel_lock(channel_id):
        if take_bot_sticky_delete(channel_id, message_id):
            return
        for sticky in await StickyMessage.for_channel(channel_id):
            if sticky.message_id == message_id:
                await sticky.release()
                return


@ipy.listen(ipy.events.MessageDelete)
async def on_sticky_deleted(event: ipy.events.MessageDelete) -> None:
    channel_id = getattr(event.message, "_channel_id", None)
    message_id = getattr(event.message, "id", None)
    if channel_id in (None, ipy.MISSING) or message_id in (None, ipy.MISSING):
        return
    await release_deleted_sticky(str(channel_id), str(message_id))


@ipy.listen(ipy.events.MessageDeleteBulk)
async def on_sticky_bulk_deleted(event: ipy.events.MessageDeleteBulk) -> None:
    channel_id = str(event.channel_id)
    for message_id in event.ids:
        await release_deleted_sticky(channel_id, str(message_id))


@ipy.listen(ipy.events.Startup)
async def on_start():
    print('Logged in as')
    print(bot.user.username)
    print(bot.user.id)
    print(ipy.__version__)
    print(coc.__version__)
    print('------')

    # await bot.user.edit(banner="bot_banner.jpg")


@coc.ClientEvents.raid_weekend_end()
async def on_raid_weekend_end():
    await clear_all_base_store_slots(bot=bot)

    channel = await bot.fetch_channel(RAID_CHANNEL)

    for message in await channel.fetch_messages():
        if message.interaction and int(message.interaction.bot.user.id) == int(bot.user.id):
            await message.delete()


async def backup_collection(src_db, dst_db, coll_name):
    src_coll = src_db[coll_name]
    dst_coll = dst_db[coll_name]

    async for doc in src_coll.find({}):
        await dst_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    src_ids = await src_coll.distinct("_id")
    await dst_coll.delete_many({"_id": {"$nin": src_ids}})


async def sync_database(src_db, dst_db):
    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, dst_db, coll)


@ipy.Task.create(ipy.IntervalTrigger(hours=2))
async def backup_database1(client: AsyncMongoClient):
    src_db = client[DATA_BASES[VERSION]]

    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, client["elo_backup1"], coll)


@ipy.Task.create(ipy.IntervalTrigger(hours=24))
async def backup_database2(client: AsyncMongoClient):
    src_db = client[DATA_BASES[VERSION]]

    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, client["elo_backup2"], coll)


async def main():
    start_time = time.time()

    try:
        print("➤ Logging into CoC API...")
        coc_start = time.time()
        coc_client = coc.EventsClient(key_names=COC_API_KEYS[VERSION])
        coc_client.add_events(on_raid_weekend_end)
        await coc_client.login(COC_API_EMAIL, COC_API_PASSWORD)
        print(f"  ✓ CoC API login completed in {time.time() - coc_start:.2f}s")
    except coc.InvalidCredentials as error:
        exit(error)

    print("➤ Connecting to MongoDB...")
    db_start = time.time()
    db_client = AsyncMongoClient(
        MONGO_DB_URL,
        tls=True,
        tlsCAFile=certifi.where(),
        uuidRepresentation="standard",
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=10000
    )
    print(f"  ✓ MongoDB connection established in {time.time() - db_start:.2f}s")

    if VERSION == "beta" and BETA_SYNC_FROM_MAIN:
        print("➤ Syncing beta database from main...")
        sync_start = time.time()
        await sync_database(db_client[DATA_BASES["main"]], db_client[DATA_BASES["beta"]])
        print(f"  ✓ Beta database sync completed in {time.time() - sync_start:.2f}s")
    elif VERSION == "beta":
        print("➤ Beta database sync is disabled (`BETA_SYNC_FROM_MAIN=False`).")
    
    print("➤ Initializing Beanie...")
    beanie_start = time.time()
    await init_beanie(
        database=db_client.get_database(name=DATA_BASES[VERSION]),
        document_models=[
            Match,
            Clan,
            HelperTask,
            BaseLayout,
            Queue,
            Player,
            RecordedMatch,
            Season,
            EloFinal,
            Helper,
            ServerState,
            OverrideCode,
            PendingBaseSubmission,
            AuditLogEntry,
            Giveaway,
            StickyMessage,
            BaseExtractionRun,
            BaseFingerprintTask,
            FinalMatchResult,
            FinalPlayerProfile,
            FinalPrediction,
            TokenWallet,
            TokenLedgerEntry,
            FinalBettingMarket,
            FinalBet,
        ],
        skip_indexes=True
    )
    await OverrideCode.get_pymongo_collection().create_indexes(OVERRIDE_CODE_INDEXES)
    await register_final_indexes()
    await AuditLogEntry.get_pymongo_collection().create_indexes(AUDIT_LOG_INDEXES)
    await Giveaway.get_pymongo_collection().create_indexes(GIVEAWAY_INDEXES)
    await StickyMessage.get_pymongo_collection().create_index("channel_id")
    await BaseLayout.get_pymongo_collection().create_indexes(BASE_FINGERPRINT_INDEXES)
    await BaseFingerprintTask.get_pymongo_collection().create_indexes(FINGERPRINT_TASK_INDEXES)
    await BaseExtractionRun.get_pymongo_collection().create_indexes(EXTRACTION_RUN_INDEXES)
    await ServerState.current()
    print(f"  ✓ Beanie initialized in {time.time() - beanie_start:.2f}s")

    # back up logic
    if VERSION == "main":
        print("➤ Starting backup tasks...")
        backup_start = time.time()
        initial_backup = asyncio.create_task(backup_database1(db_client))
        initial_backup.set_name("initial_backup_task")
        backup_database1.start(db_client)
        backup_database2.start(db_client)
        print(f"  ✓ Backup tasks started in {time.time() - backup_start:.2f}s")

    # bot fields
    bot.coc_client = coc_client
    bot.file_locks = defaultdict(asyncio.Lock)
    bot.coc_client_lock = asyncio.Lock()
    bot.match_id_choices = {}
    bot.builder_names = {}
    bot.base_fingerprints = FingerprintService(bot, DATA_BASES[VERSION])

    # load cogs
    print("➤ Loading cogs...")
    cogs_start = time.time()
    for extension in get_extensions("cogs"):
        bot.load_extension(extension)
    print(f"  ✓ Cogs loaded in {time.time() - cogs_start:.2f}s")

    print(f"➤ Total startup time: {time.time() - start_time:.2f}s")
    print("➤ Starting bot...")
    try:
        await bot.astart()
    finally:
        await bot.base_fingerprints.close()


if __name__ == "__main__":
    # import logging
    # logging.basicConfig(level=logging.DEBUG)
    # logging.getLogger('coc.http').setLevel(logging.DEBUG)
    asyncio.run(main())

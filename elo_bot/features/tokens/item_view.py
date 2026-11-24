from uuid import uuid4
from collections import Counter
from time import time

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI, TOKEN_ITEM_EMOJIS
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.token_items import ITEMS, TokenItem
from elo_bot.db.token_models import TokenItemUse, TokenWallet

PAGE_SIZE = 3
PAGES = -(-len(ITEMS) // PAGE_SIZE)


def challenge_code_receipt(uses: list[TokenItemUse]) -> str:
    now = int(time())
    codes = [(use.target, use.expires_at) for use in uses if use.state == "armed" and use.target
             and use.expires_at is not None and use.expires_at > now]
    if not codes:
        return f"{ERROR_EMOJI} This use has no valid Challenge Override codes left."
    first_code, expires_at = codes[0]
    expiry = ipy.Timestamp.fromtimestamp(expires_at).format(ipy.TimestampStyles.RelativeTime)
    if len(codes) == 1:
        return (f"{SUCCESS_EMOJI} Save `{first_code}` for the `override_code` option in `/challenge send`. "
                f"It expires {expiry}.")
    code_lines = "\n".join(code for code, _ in codes)
    return (f"{SUCCESS_EMOJI} Save these codes for the `override_code` option in `/challenge send`.\n"
            f"They expire {expiry}.\n```\n{code_lines}\n```")


def activation_details(uses: list[TokenItemUse], *, hidden: bool) -> str:
    counts = Counter("Queued" if use.state == "queued" else "Pending" if use.item is TokenItem.EXP
                     else "Ready" if use.state == "armed" else "Reserved" for use in uses)
    details = " · ".join(f"{status}: `{count:,}`" if count > 1 else status for status, count in counts.items())
    code = next((use.target for use in uses if use.item is TokenItem.CHALLENGE and use.state == "armed"), None)
    if hidden and code:
        details += f" · `{code}`"
    return details


async def active_item_panel(wallet: TokenWallet, *, hidden: bool = False, page: int = 0) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    uses = await TokenItemUse.find({"wallet_id": wallet.id, "active": True}).sort(
        [("activation_order", 1), ("_id", 1)]).to_list()
    embed = ipy.Embed(title="**Activated Items**", color=embed_color())
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    groups = {}
    positions = Counter()
    available = []
    for use in uses:
        groups.setdefault((use.item, use.district), []).append(use)
        positions[use.slot] += 1
        if use.state == "queued" or use.state == "armed" and use.item is not TokenItem.EXP:
            available.append((use, positions[use.slot]))
    for (item, district), activations in groups.items():
        listing = ITEMS[item]
        emoji = TOKEN_ITEM_EMOJIS.get(item)
        name = listing.name + (f" · {district}" if district else "")
        embed.add_field(name=(f"{emoji} " if emoji else "") + f"**{name}**",
                        value=activation_details(activations, hidden=hidden))
    pages = max(1, -(-len(available) // 25))
    page = min(page, pages - 1)
    options = [ipy.StringSelectOption(
        label=f"{ITEMS[use.item].name} · #{position}", value=use.id, emoji=TOKEN_ITEM_EMOJIS.get(use.item),
        description=("Queued" if use.state == "queued" else "Ready") + (f" · {use.district}" if use.district else ""))
        for use, position in available[page * 25:(page + 1) * 25]]
    controls = []
    if not uses:
        embed.description = "You have no activated items."
    elif options:
        embed.description = "Items are used in activation order. Select an unused activation to return it to inventory."
    else:
        embed.description = "Reserved and pending items cannot be disabled."
    if options:
        controls.append(ipy.ActionRow(ipy.StringSelectMenu(
            *options, custom_id=f"tokenactive|disable|{wallet.user_id}|{page}", placeholder="Select one activation to disable")))
    buttons = [ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Refresh",
                           custom_id=f"tokenactive|refresh|{wallet.user_id}|{page}")]
    if pages > 1:
        buttons.extend([
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 0,
                       custom_id=f"tokenactive|page|{wallet.user_id}|{max(0, page - 1)}"),
            ipy.Button(style=ipy.ButtonStyle.SECONDARY, label=f"{page + 1}/{pages}", disabled=True,
                       custom_id=f"tokenactive|page|{wallet.user_id}|{page}"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page == pages - 1,
                       custom_id=f"tokenactive|page|{wallet.user_id}|{page + 1}"),
        ])
    controls.append(ipy.ActionRow(*buttons))
    return embed, controls


async def item_panel(wallet: TokenWallet, mode: str, page: int, *,
                     hidden: bool = False, viewer_id: str | None = None) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    viewer_id = viewer_id or wallet.user_id
    read_only = viewer_id != wallet.user_id
    shop = mode == "shop"
    title = "Token Shop" if shop else "Tokens Inventory"
    embed = ipy.Embed(title=f"**{title}**", color=embed_color())
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    active = {}
    if not shop:
        for use in await TokenItemUse.find({"wallet_id": wallet.id, "active": True}).sort(
                [("activation_order", 1), ("_id", 1)]).to_list():
            active.setdefault(use.item, []).append(use)
    if not shop and not active and not any(wallet.inventory.values()):
        embed.description = "No items." if read_only else "Your inventory is empty. Use `/tokens shop`."
        return embed, []
    visible = [item for item in ITEMS if shop or wallet.inventory.get(item, 0) > 0 or item in active]
    pages = max(1, -(-len(visible) // PAGE_SIZE))
    page = min(page, pages - 1)
    selected = visible[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    options = []
    if shop:
        embed.description = f"**Available:** {max(wallet.balance, 0):,} {TOKEN_EMOJI}"
    for item in selected:
        listing = ITEMS[item]
        emoji = TOKEN_ITEM_EMOJIS.get(item)
        name = (f"{emoji} " if emoji else "") + f"**{listing.name}**"
        quantity = wallet.inventory.get(item, 0)
        details = f"{listing.price:,} {TOKEN_EMOJI}" if shop else f"Owned: `{quantity:,}`"
        if item in active:
            details += f" · {activation_details(active[item], hidden=hidden and not read_only)}"
        embed.add_field(name=name, value=f"{details}\n{listing.description}")
        if not read_only and (shop or quantity):
            options.append(ipy.StringSelectOption(label=listing.name, value=item.value, emoji=emoji))
    action = uuid4().hex
    target_suffix = f"|{wallet.user_id}" if read_only else ""
    rows = []
    if options:
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(
            *options, custom_id=f"tokenitems|pick|{viewer_id}|{mode}|{page}|{action}",
            placeholder="Select an item to buy" if shop else "Select an item to use",
        )))
    rows.append(ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 0,
                   custom_id=f"tokenitems|page|{viewer_id}|{mode}|{page - 1}|{action}{target_suffix}"),
        ipy.Button(style=ipy.ButtonStyle.SECONDARY, label=f"{page + 1}/{pages}", disabled=True,
                   custom_id=f"tokenitems|page|{viewer_id}|{mode}|{page}|{action}{target_suffix}"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page + 1 == pages,
                   custom_id=f"tokenitems|page|{viewer_id}|{mode}|{page + 1}|{action}{target_suffix}"),
    ))
    return embed, rows

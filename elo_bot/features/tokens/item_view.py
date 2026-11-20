from uuid import uuid4

import interactions as ipy

from elo_bot.config.emojis import TOKEN_EMOJI, TOKEN_ITEM_EMOJIS
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.core.token_items import ITEMS, TokenItem
from elo_bot.db.token_models import TokenItemUse, TokenWallet

PAGE_SIZE = 3
PAGES = -(-len(ITEMS) // PAGE_SIZE)


async def item_panel(wallet: TokenWallet, mode: str, page: int, *,
                     hidden: bool = False) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    owner = wallet.user_id
    shop = mode == "shop"
    title = "Token Shop" if shop else "Token Inventory"
    embed = ipy.Embed(title=f"**{title}**", color=COLOR)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    selected = list(ITEMS)[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    active = {} if shop else {
        use.item: use for use in await TokenItemUse.find({"wallet_id": wallet.id, "active": True}).to_list()
    }
    if not shop and not active and not any(wallet.inventory.values()):
        embed.description = "Your inventory is empty. Use `/tokens shop`."
        return embed, []
    options = []
    if shop:
        embed.description = f"**Available:** {max(wallet.balance, 0):,} {TOKEN_EMOJI}"
    for item in selected:
        listing = ITEMS[item]
        emoji = TOKEN_ITEM_EMOJIS.get(item)
        name = (f"{emoji} " if emoji else "") + f"**{listing.name}**"
        quantity = wallet.inventory.get(item, 0)
        details = f"{listing.price:,} {TOKEN_EMOJI}" if shop else f"Owned: {quantity:,}"
        if item in active:
            details += f" · {'Ready' if active[item].state == 'armed' else 'Reserved'}"
            if hidden and item is TokenItem.CHALLENGE and active[item].state == "armed" and active[item].target:
                details += f" · `{active[item].target}`"
        embed.add_field(name=name, value=f"{details}\n{listing.description}")
        if shop or quantity:
            options.append(ipy.StringSelectOption(label=listing.name, value=item.value, emoji=emoji))
    action = uuid4().hex
    rows = []
    if options:
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(
            *options, custom_id=f"tokenitems|pick|{owner}|{mode}|{page}|{action}",
            placeholder="Select an item to buy" if shop else "Select an item to use",
        )))
    rows.append(ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 0,
                   custom_id=f"tokenitems|page|{owner}|{mode}|{page - 1}|{action}"),
        ipy.Button(style=ipy.ButtonStyle.SECONDARY, label=f"{page + 1}/{PAGES}", disabled=True,
                   custom_id=f"tokenitems|page|{owner}|{mode}|{page}|{action}"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page + 1 == PAGES,
                   custom_id=f"tokenitems|page|{owner}|{mode}|{page + 1}|{action}"),
    ))
    return embed, rows

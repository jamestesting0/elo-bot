import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.db import BaseLayout, Player
from elo_bot.discord.members import member_guild
from elo_bot.features.bases.layouts.ui import base_embed_names, construct_base_embed


async def update_private_base_message(ctx: ipy.ComponentContext, district: str, index: int):
    query = BaseLayout.find(BaseLayout.quality == "Private", BaseLayout.builder_id == str(ctx.author.id))
    if district != "Any":
        query = query.find(BaseLayout.district == district)

    bases = await query.sort("_id").to_list()

    if not bases:
        await ctx.edit(ctx.message, content=f"{ERROR_EMOJI} No private bases found.", embeds=[], components=[])
        return

    index = min(max(index, 0), len(bases) - 1)
    base = bases[index]

    player = await Player.find_one(Player.user_id == str(ctx.author.id), fetch_links=True)
    is_preset = False
    if player and base.district in player.base_presets:
        preset_base = player.base_presets[base.district]
        is_preset = preset_base and preset_base.link == base.link

    left_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="◀️️",
        custom_id=f"p_base_left|{district}|{index - 1}",
        disabled=index == 0,
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
    embed = construct_base_embed(base, "Performance", names=await base_embed_names(guild, base, "Performance"))
    await ctx.edit(ctx.message, embed=embed, components=ipy.ActionRow(
        left_button, right_button, preset_button, remove_button
    ))


async def find_private_base_action(
        ctx: ipy.ComponentContext, district: str, base_id: str | None,
) -> BaseLayout | None:
    query: dict[str, str | PydanticObjectId] = {
        "quality": "Private", "builder_id": str(ctx.author.id),
    }
    if base_id:
        query["_id"] = PydanticObjectId(base_id)
    else:
        if not ctx.message.embeds or not ctx.message.embeds[0].description:
            return None
        query["link"] = ctx.message.embeds[0].description.splitlines()[0]
    if district != "Any":
        query["district"] = district
    return await BaseLayout.find_one(query)


async def clear_private_base_preset(player: Player, base: BaseLayout) -> None:
    field = f"base_presets.{base.district}"
    await player.get_pymongo_collection().update_one(
        {"_id": player.id, "$or": [{f"{field}._id": base.id}, {field: base.to_ref()}]},
        {"$set": {field: None}},
    )

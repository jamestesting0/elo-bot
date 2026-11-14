import asyncio
from hashlib import sha256
from io import BytesIO

import anyio
import interactions as ipy

from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.discord.message_edits import edit_channel_message
from elobot.features.finals.lifecycle import resolve_final
from elobot.features.finals.rendering import bracket_text, render_bracket
from elobot.core.finals import FinalError
from elobot.discord.mentions import mention_user
from elobot.db import EloFinal


PUBLICATION_LOCKS: dict[str, anyio.Lock] = {}


def bracket_embed(final: EloFinal, names: dict[str, str] | None = None) -> ipy.Embed:
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Bracket**", color=COLOR,
                      description=bracket_text(final, names))
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


async def publish_bracket(bot: ipy.Client, final: EloFinal) -> ipy.Message:
    async with PUBLICATION_LOCKS.setdefault(str(final.id), anyio.Lock()):
        final = await resolve_final(final.season_number)
        collection = EloFinal.get_pymongo_collection()
        channel = await bot.fetch_channel(final.channel_id)
        user_ids = {user for node in final.nodes.values() for user in (*node.contestants, node.winner) if user}
        members = await asyncio.gather(*(channel.guild.fetch_member(user) for user in user_ids))
        names = {user: mention_user(member, user) for user, member in zip(user_ids, members)}
        embed = bracket_embed(final, names)
        render_error = None
        try:
            rendered = await asyncio.to_thread(render_bracket, final)
            embed.set_image(url="attachment://bracket.png")
        except FinalError as error:
            rendered = None
            render_error = str(error)
            embed.add_field(name="**Image**", value=render_error)
        message = None
        try:
            if final.bracket_message_id:
                message = await channel.fetch_message(int(final.bracket_message_id))
            elif final.bracket_send_attempted:
                messages = await channel.fetch_messages(limit=100)
                message = next((item for item in messages if str(item.nonce) == final.bracket_send_nonce
                                and item.author.id == bot.user.id), None)
                if message is None:
                    raise FinalError("Bracket publication is uncertain. A manager must locate the earlier message.")
            nonce = final.bracket_send_nonce or sha256(f"final-bracket|{final.id}".encode()).hexdigest()[:24]
            claimed = await collection.update_one({"_id": final.id, "revision": final.revision}, {"$set": {
                "publication_pending": True, "bracket_send_attempted": True, "bracket_send_nonce": nonce,
            }})
            if not claimed.matched_count:
                raise FinalError("The bracket changed while rendering. Refresh to publish its current state.")
            with BytesIO(rendered or b"") as image:
                file = ipy.File(image, file_name="bracket.png") if rendered else None
                if message is None:
                    message = await channel.send(embed=embed, file=file, nonce=nonce, enforce_nonce=True)
                else:
                    await edit_channel_message(message, embed=embed, attachments=[], file=file)
            bound = await collection.update_one({"_id": final.id}, {"$set": {"bracket_message_id": str(message.id)}})
            if not bound.matched_count:
                raise FinalError("The bracket was sent, but its saved message reference is missing.")
            await message.pin()
            published = await collection.update_one({"_id": final.id, "revision": final.revision}, {"$set": {
                "bracket_published_revision": final.revision, "publication_pending": bool(render_error),
                "publication_error": render_error,
            }})
            if not published.matched_count:
                raise FinalError("The bracket changed during publication. Its update remains pending.")
            return message
        except (ipy.errors.HTTPException, FinalError) as error:
            await collection.update_one({"_id": final.id}, {"$set": {
                "publication_pending": True, "publication_error": str(error),
            }})
            raise FinalError("Bracket publication is incomplete. Use `/final recover`.") from error

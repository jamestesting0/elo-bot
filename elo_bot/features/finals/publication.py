from hashlib import sha256
from io import BytesIO

import anyio
import interactions as ipy
import json

from elo_bot.features.finals.rendering import render_bracket
from elo_bot.core.finals import FinalError
from elo_bot.discord.mentions import fetch_members, member_usernames
from elo_bot.db import EloFinal
from elo_bot.features.finals.bracket_ui import bracket_controls, bracket_embed
from elo_bot.features.finals.lifecycle import refresh_display_names, reload_final
from elo_bot.features.finals.labels import final_player_link
from elo_bot.features.finals.posts import PUBLICATION_LOCKS, PostContent, PostReference, publish_post
from elo_bot.features.finals.predictions import submitted_predictions


async def bracket_signature(final: EloFinal) -> str:
    """Fingerprint everything the public bracket post shows, including its predictions line."""
    content = {"presentation": 5, "season": final.season_number, "state": final.state, "champion": final.champion,
               "names": final.player_nicknames, "nickname_overrides": sorted(final.nickname_overrides), "active": final.active,
               "predictions": [final.predictions_ever_opened, final.prediction_state, await submitted_predictions(final)],
               "nodes": {key: node.model_dump(mode="json") for key, node in final.nodes.items()}}
    return sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


async def publish_bracket(bot: ipy.Client, final: EloFinal) -> ipy.Message:
    async with PUBLICATION_LOCKS.setdefault(str(final.id), anyio.Lock()):
        final = await reload_final(final)
        collection = EloFinal.get_pymongo_collection()
        try:
            channel = await bot.fetch_channel(final.channel_id)
            guild = getattr(channel, "guild", None)
            if guild is None:
                raise FinalError("The Final channel is unavailable.")
            members = await fetch_members(guild, final.entrant_ids)
            final = await refresh_display_names(final, members)
            signature = await bracket_signature(final)
            submitted = await submitted_predictions(final)
            content_filter = {"_id": final.id, "state": final.state, "champion": final.champion,
                              "player_nicknames": final.player_nicknames, "active": final.active,
                              "nickname_overrides": final.nickname_overrides or {"$in": [[], None]},
                              "prediction_state": final.prediction_state,
                              "nodes": {key: node.model_dump() for key, node in final.nodes.items()}}
            names = {user: final_player_link(final, user, member) for user, member in members.items()}
            embed = bracket_embed(final, names, submitted)
            render_error = None
            rendered = None
            if final.nodes:
                try:
                    rendered = await anyio.to_thread.run_sync(render_bracket, final, None, None,
                                                              member_usernames(members))
                    embed.set_image(url="attachment://bracket.png")
                except FinalError as error:
                    render_error = str(error)
                    embed.add_field(name="**Image**", value=render_error)
            nonce = final.bracket_send_nonce or sha256(f"final-bracket|{final.id}".encode()).hexdigest()[:24]
            claimed = await collection.update_one(content_filter, {"$set": {
                "publication_pending": True, "bracket_send_attempted": True, "bracket_send_nonce": nonce,
            }})
            if not claimed.matched_count:
                raise FinalError("The bracket changed while rendering. Refresh to publish its current state.")
            reference = PostReference(nonce, bot.user.id, final.bracket_message_id, final.bracket_send_attempted)
            with BytesIO(rendered or b"") as image:
                file = ipy.File(image, file_name="bracket.png") if rendered else None
                message = await publish_post(channel, reference, PostContent(embed, await bracket_controls(final), file))
            await collection.update_one({"_id": final.id}, {"$set": {"bracket_message_id": str(message.id)}})
            await message.pin()
            if await submitted_predictions(final) != submitted:
                raise FinalError("Predictions changed during publication. Its update remains pending.")
            published = await collection.update_one(content_filter, {"$set": {
                "bracket_signature": signature, "publication_pending": bool(render_error),
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

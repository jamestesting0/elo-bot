from hashlib import sha256
from io import BytesIO
from typing import assert_never

import anyio
import interactions as ipy
from aiohttp import ClientError
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.mentions import member_usernames, mention_user
from elo_bot.features.finals.lifecycle import refresh_display_names, reload_final
from elo_bot.features.finals.posts import PUBLICATION_LOCKS, PostContent, PostReference, publish_post
from elo_bot.features.finals.rendering import render_scoreboard
from elo_bot.features.finals.result_ui import result_embed


async def publish_result(bot, final: EloFinal, result: FinalMatchResult) -> None:
    key = result.id.replace(".", "_")
    async with PUBLICATION_LOCKS.setdefault(f"result:{final.id}:{result.node_id}", anyio.Lock()):
        final = await reload_final(final)
        node = final.nodes.get(result.node_id)
        if node is None or node.result_id != result.id:
            return
        progress = final.result_progress[key]
        if (progress.published and not progress.error
                and progress.published_repair_pending == final.repair_pending
                and progress.public_message_id
                and progress.public_published_repair_pending == final.repair_pending):
            return
        collection = EloFinal.get_pymongo_collection()
        path = f"result_progress.{key}"
        # A correction reuses its predecessor's posts, including an uncertain send.
        # Read fresh progress under the match lock: the earlier send may have finished
        # after the correction transaction created its new progress entry.
        previous_id = result.previous_id
        thread_found = bool(progress.send_attempted or progress.message_id)
        public_found = bool(progress.public_send_attempted or progress.public_message_id)
        while previous_id and not (thread_found and public_found):
            previous = final.result_progress[previous_id.replace(".", "_")]
            if not thread_found and (previous.send_attempted or previous.message_id):
                progress.message_id = previous.message_id
                progress.send_attempted = previous.send_attempted
                progress.send_nonce = previous.send_nonce or sha256(f"final-result:{previous_id}".encode()).hexdigest()[:24]
                progress.channel_id = previous.channel_id
                progress.thread_deleted = previous.thread_deleted
                thread_found = True
            if not public_found and (previous.public_send_attempted or previous.public_message_id):
                progress.public_message_id = previous.public_message_id
                progress.public_send_attempted = previous.public_send_attempted
                progress.public_send_nonce = previous.public_send_nonce
                public_found = True
            if thread_found and public_found:
                break
            previous_result = await FinalMatchResult.get(previous_id)
            previous_id = previous_result.previous_id
        channel_id = progress.channel_id or result.source_thread or str(final.channel_id)
        failures = []
        channels = []
        shared = channel_id == str(final.channel_id)
        if not shared:
            try:
                channels.append(("thread", await bot.fetch_channel(int(channel_id))))
            except ipy.errors.NotFound:
                progress.message_id = None
                progress.send_attempted = False
                progress.published_repair_pending = None
                progress.thread_deleted = True
                channel_id = str(final.channel_id)
                shared = True
                await collection.update_one({"_id": final.id}, {"$set": {f"{path}.thread_deleted": True,
                    f"{path}.message_id": None, f"{path}.send_attempted": False, f"{path}.channel_id": channel_id,
                    f"{path}.published_repair_pending": None}})
            except ipy.errors.HTTPException as error:
                failures.append(str(error))
        try:
            channels.append(("shared" if shared else "public", await bot.fetch_channel(final.channel_id)))
        except ipy.errors.HTTPException as error:
            failures.append(str(error))
        names, avatars, members = {}, {}, {}
        rendered, render_error = None, None
        if channels:
            guild = channels[0][1].guild
            for user in result.snapshot.contestants:
                member = None
                try:
                    with anyio.fail_after(5):
                        member = await guild.fetch_member(user)
                except (ipy.errors.HTTPException, ClientError, TimeoutError, OSError):
                    member = None
                members[user] = member
                names[user] = mention_user(member, user)
                match result.snapshot.outcome:
                    case "Played":
                        if member and member.display_avatar:
                            try:
                                with anyio.fail_after(5):
                                    avatars[user] = await member.display_avatar.fetch(extension=".png", size=256)
                            except (ipy.errors.HTTPException, ClientError, TimeoutError, OSError):
                                avatars.pop(user, None)
                    case "Forfeit":
                        continue
                    case unreachable:
                        assert_never(unreachable)
            final = await refresh_display_names(final, members)
            match result.snapshot.outcome:
                case "Played":
                    try:
                        rendered = await anyio.to_thread.run_sync(render_scoreboard, final, result.snapshot, avatars,
                                                                  member_usernames(members))
                    except FinalError as error:
                        render_error = str(error)
                case "Forfeit":
                    rendered = None
                case unreachable:
                    assert_never(unreachable)
        refresh_image = bool(progress.image_error)
        for destination, channel in channels:
            public = destination == "public"
            message_id = progress.public_message_id if public else progress.message_id
            attempted = progress.public_send_attempted if public else progress.send_attempted
            nonce = progress.public_send_nonce if public else progress.send_nonce
            published_repair = progress.public_published_repair_pending if public else progress.published_repair_pending
            if destination == "shared" and progress.public_message_id:
                message_id, attempted, nonce = (progress.public_message_id, progress.public_send_attempted,
                                                progress.public_send_nonce)
            if message_id and published_repair == final.repair_pending and not refresh_image:
                continue
            nonce_key = f"final-result:{result.id}" if attempted else f"final-result:{result.id}:{destination}"
            nonce = nonce or sha256(nonce_key.encode()).hexdigest()[:24]
            prefix = "public_" if public else ""
            claim = {f"{path}.{prefix}send_attempted": True, f"{path}.{prefix}send_nonce": nonce,
                     f"{path}.{prefix}message_id": message_id}
            if not public:
                claim[f"{path}.channel_id"] = channel_id
                claim[f"{path}.thread_deleted"] = progress.thread_deleted
            try:
                claimed = await collection.update_one({"_id": final.id, f"nodes.{result.node_id}.result_id": result.id},
                                                      {"$set": claim})
                if not claimed.matched_count:
                    raise FinalError("The result changed during publication. Its update remains pending.")
                embed = result_embed(final, result, names, scoreboard=bool(rendered))
                if rendered:
                    embed.set_image(url="attachment://scoreboard.png")
                elif render_error:
                    embed.add_field(name="**Image**", value=render_error)
                with BytesIO(rendered or b"") as image:
                    file = ipy.File(image, file_name="scoreboard.png") if rendered else None
                    message = await publish_post(channel, PostReference(nonce, bot.user.id, message_id, attempted),
                                                 PostContent(embed, [], file))
                updates = {f"{path}.{prefix}message_id": str(message.id),
                           f"{path}.{prefix}published_repair_pending": final.repair_pending}
                if destination == "shared":
                    updates.update({f"{path}.public_message_id": str(message.id),
                        f"{path}.public_send_attempted": True, f"{path}.public_send_nonce": nonce,
                        f"{path}.public_published_repair_pending": final.repair_pending})
                await collection.update_one({"_id": final.id}, {"$set": updates})
            except (ipy.errors.HTTPException, FinalError, PyMongoError) as error:
                failures.append(str(error))
        await collection.update_one({"_id": final.id}, {"$set": {
            f"{path}.published": not failures, f"{path}.image_error": render_error,
            f"{path}.error": "; ".join(failures) if failures else render_error}})
        if failures:
            raise FinalError(f"{result.node_id} result saved; publication remains pending.")
        if channels:
            if result.source_match_id:
                await Match.get_pymongo_collection().update_one({"_id": PydanticObjectId(result.source_match_id),
                    "final_result_id": result.id}, {"$set": {"final_result_operation.published": True,
                                                            "final_result_operation.publication_error": None}})

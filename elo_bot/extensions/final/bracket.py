import re

import interactions as ipy
from interactions import SlashContext
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.discord.members import member_guild
from elo_bot.features.finals.labels import final_member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import bracket_group, final
from elo_bot.features.finals import lifecycle
from elo_bot.features.finals.bracket import automatic_pairs, require_lineup_confirmation
from elo_bot.features.finals.bracket_editor import editor_view, swapped_pairs
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.registration_posts import sync_registration_posts


class Bracket:
    final = final
    bracket_group = bracket_group

    @bracket_group.subcommand(sub_cmd_name="reset", sub_cmd_description="Reset an unused bracket")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_bracket_reset(self, ctx: SlashContext):
        await lifecycle.reset_bracket(await self._ongoing_edition(ctx, access="manager"))
        await ctx.send(f"{SUCCESS_EMOJI} Bracket reset. Manage entrants with `/final dashboard`.", ephemeral=True)

    @bracket_group.subcommand(sub_cmd_name="create", sub_cmd_description="Edit and confirm the seeded bracket")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_bracket_create(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        await self._open_bracket_editor(ctx, edition)

    async def _open_bracket_editor(self, ctx, edition, *, edit=False):
        owner = str(ctx.author.id)
        if edition.state != "REGISTRATION":
            raise FinalError("This view is outdated. Run `/final bracket create` again.")
        require_lineup_confirmation(edition)
        draft_owner = lifecycle.current_pairing_draft_owner(edition)
        if draft_owner not in {None, owner}:
            names = await final_member_names(edition, await member_guild(ctx), [draft_owner])
            raise FinalError(f"{names[draft_owner]} is editing the bracket.")
        pairs = edition.manual_draft if edition.draft_setup_revision == edition.setup_revision else automatic_pairs(edition)
        edition = await lifecycle.save_pairing_draft(edition, owner, pairs)
        names = await final_member_names(edition, await member_guild(ctx), edition.entrant_ids)
        embed, controls = editor_view(edition, names)
        if edit:
            await ctx.edit_origin(content="", embed=embed, components=controls)
        else:
            await ctx.send(embed=embed, components=controls, ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalpair\|.+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_pairing_action(self, ctx: ipy.ComponentContext):
        parts = ctx.custom_id.split("|")
        edition = await self._ongoing_edition(ctx, parts[1], access="manager")
        if len(parts) != 5:
            raise FinalError("This view is outdated. Run `/final bracket create` again.")
        _, final_id, owner, revision, action = parts
        if owner != str(ctx.author.id) or edition.draft_owner != owner:
            raise FinalError("You cannot interact with another user's components.")
        if (int(revision) != edition.draft_revision or edition.state != "REGISTRATION"
                or edition.draft_setup_revision != edition.setup_revision):
            raise FinalError("This view is outdated. Run `/final bracket create` again.")
        match action:
            case "swap":
                pairs = swapped_pairs(edition, ctx.values)
                edition = await lifecycle.save_pairing_draft(edition, owner, pairs)
            case "reset":
                edition = await lifecycle.save_pairing_draft(edition, owner, automatic_pairs(edition))
            case "cancel":
                await lifecycle.update_setup(edition, {"draft_owner": None, "manual_draft": {},
                                                       "draft_revision": edition.draft_revision + 1})
                await ctx.edit_origin(content=f"{SUCCESS_EMOJI} Bracket draft discarded.", embeds=[], components=[])
                return
            case "confirm":
                await ctx.defer(edit_origin=True)
                edition = await lifecycle.confirm_bracket(edition, owner, manual=True)
                try:
                    message = await publish_bracket(self.bot, edition)
                    content = f"{SUCCESS_EMOJI} Bracket confirmed and published: {message.jump_url}"
                except (FinalError, PyMongoError) as error:
                    content = f"{IMPORTANT_EMOJI} Bracket confirmed. Publishing failed and will retry: {error}"
                await self._show_dashboard(ctx, await lifecycle.reload_final(edition), outcome=content, edit=True)
                await sync_registration_posts(self.bot, edition)
                return
            case _:
                raise FinalError("This view is outdated. Run `/final bracket create` again.")
        guild = await member_guild(ctx)
        names = await final_member_names(edition, guild, edition.entrant_ids)
        embed, controls = editor_view(edition, names)
        await ctx.edit_origin(embed=embed, components=controls)

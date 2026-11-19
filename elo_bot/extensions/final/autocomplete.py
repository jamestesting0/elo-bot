import interactions as ipy

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.extensions.final.betting import Betting
from elo_bot.features.finals.labels import pairing_label


class Autocomplete:
    @ipy.global_autocomplete(option_name="final_season")
    async def final_season_autocomplete(self, ctx: ipy.AutocompleteContext):
        finals = {row["season_number"] for row in await EloFinal.get_pymongo_collection().find(
            {}, {"season_number": 1}).to_list()}
        typed = ctx.input_text.strip()
        choices = [ipy.SlashCommandChoice(name=f"Season {number} Final", value=number)
                   for number in sorted(finals, reverse=True) if typed in str(number)]
        await ctx.send(choices=choices[:25])

    async def _betting_match_choices(self, ctx: ipy.AutocompleteContext, *, for_void: bool) -> None:
        try:
            edition = await self._ongoing_edition(ctx)
            states = ["OPEN", "CLOSED"] if for_void else ["OPEN"]
            rows = await FinalBettingMarket.find({"final_id": str(edition.id),
                "state": {"$in": states}}).to_list()
            allowed = {(row.node_id, row.contestant_revision) for row in rows}
            nodes = [node for node in edition.nodes.values() if not node.bye and all(node.contestants)
                     and (not for_void or node.winner is None) and (node.id, node.contestant_revision) in allowed]
            choices = [ipy.SlashCommandChoice(name=pairing_label(edition, node)[:100], value=node.id)
                       for node in nodes if ctx.input_text.casefold() in pairing_label(edition, node).casefold()]
        except FinalError:
            choices = []
        await ctx.send(choices=choices[:25])

    @Betting.final_betting_close.autocomplete("match")
    async def final_close_autocomplete(self, ctx: ipy.AutocompleteContext):
        await self._betting_match_choices(ctx, for_void=False)

    @Betting.final_betting_void.autocomplete("match")
    async def final_void_autocomplete(self, ctx: ipy.AutocompleteContext):
        await self._betting_match_choices(ctx, for_void=True)

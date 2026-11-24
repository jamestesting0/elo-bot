from unittest.mock import AsyncMock, MagicMock

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.predictions import (
    PredictionAction, apply_prediction_action, begin_prediction, change_prediction_state, normalize_picks,
    pick_options,
)
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalPredictionsView(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await change_prediction_state(await confirm_bracket(self.final, "9"), True)
        for user, choice in (("9", 0), ("8", 1)):
            prediction = await begin_prediction(self.final, user)
            picks = {}
            for key in self.final.nodes:
                options = pick_options(self.final, picks, key)
                picks[key] = options[min(choice, len(options) - 1)]
            await prediction.set({"draft": normalize_picks(self.final, picks), "stage": 3})
            await apply_prediction_action(PredictionAction(final_id=self.final.id, user_id=user,
                session_revision=prediction.session_revision, action="submit"))
        await begin_prediction(self.final, "7")

    def viewer(self, user=9, *, manager=False, component=False):
        ctx = self.context(user, manager=manager, component=component)
        ctx.guild.fetch_member.side_effect = lambda member: MagicMock(id=int(member), display_name=f"Display {member}")
        return ctx

    async def view(self, ctx, **options):
        await self.cog.final_predictions_view.callback.func(self.cog, ctx, **options)
        return ctx.send.await_args

    async def test_players_wait_for_closure_and_managers_see_picks_privately_before_it(self):
        sent = await self.view(self.viewer(manager=False))
        self.assertEqual(sent.args[0], f"{ERROR_EMOJI} Predictions are visible once they close.")
        manager = self.viewer(manager=True)
        sent = await self.view(manager, hidden=False)
        manager.defer.assert_awaited_once_with(ephemeral=True)
        lines = sent.kwargs["embed"].description.splitlines()
        self.assertEqual(lines[0], "Open · 2 submitted")
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(" · 🏆 " in line for line in lines[2:]))
        menu = sent.kwargs["components"][0].components[0]
        self.assertEqual(sorted(option.value for option in menu.options), ["8", "9"])

    async def test_after_closure_anyone_opens_cards_privately_from_a_shared_list(self):
        await change_prediction_state(self.final, False)
        player = self.viewer()
        sent = await self.view(player)
        player.defer.assert_awaited_once_with(ephemeral=False)
        menu = sent.kwargs["components"][0].components[0]
        for user in (9, 5):
            with self.subTest(user=user):
                clicker = self.viewer(user, component=True)
                clicker.custom_id, clicker.values = menu.custom_id, ["8"]
                await self.cog.final_predictions_pick.callback.func(self.cog, clicker)
                clicker.defer.assert_awaited_once_with(ephemeral=True)
                card = clicker.send.await_args.kwargs
                self.assertTrue(card["ephemeral"])
                self.assertEqual(card["embed"].author.name, "Display 8")
                self.assertEqual(card["embed"].author.url, "https://discord.com/users/8")
                self.assertFalse(card["embed"].description)
                self.assertEqual(card["file"].file_name, "prediction.png")
                self.assertEqual([field.name for field in card["embed"].fields], [])
                clicker.edit.assert_not_awaited()

    async def test_player_option_shows_one_card_and_rejects_unsubmitted_drafts(self):
        await change_prediction_state(self.final, False)
        sent = await self.view(self.viewer(), player=MagicMock(id=8), hidden=True)
        self.assertTrue(sent.kwargs["ephemeral"])
        self.assertEqual(sent.kwargs["embed"].author.url, "https://discord.com/users/8")
        self.assertEqual(sent.kwargs["embed"].image.url, "attachment://prediction.png")
        sent = await self.view(self.viewer(), player=MagicMock(id=7))
        self.assertEqual(sent.args[0], f"{ERROR_EMOJI} This player didn't submit a prediction.")

    async def test_final_season_autocomplete_offers_each_season_with_a_final(self):
        async def choices(typed=""):
            ctx = MagicMock(input_text=typed, send=AsyncMock())
            await self.cog.final_season_autocomplete.callback(self.cog, ctx)
            return [(str(choice.name), choice.value) for choice in ctx.send.await_args.kwargs["choices"]]

        season = self.final.season_number
        self.assertEqual(await choices(), [(f"Season {season} Final", season)])
        self.assertEqual(await choices("9"), [])

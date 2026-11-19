import interactions as ipy

from elo_bot.extensions.final import groups
from elo_bot.extensions.final.autocomplete import Autocomplete
from elo_bot.extensions.final.betting import Betting
from elo_bot.extensions.final.bracket import Bracket
from elo_bot.extensions.final.checks import FinalChecks
from elo_bot.extensions.final.confirmations import Confirmations
from elo_bot.extensions.final.dashboard import Dashboard
from elo_bot.extensions.final.history import History
from elo_bot.extensions.final.predictions import Predictions
from elo_bot.extensions.final.results import Results
from elo_bot.extensions.final.roster import Roster
from elo_bot.extensions.final.rounds import Rounds
from elo_bot.extensions.final.setup import Setup
from elo_bot.extensions.final.status import Status
from elo_bot.extensions.final.views import Views


class FinalCommands(Status, Dashboard, Roster, Confirmations, Views, History, Setup, Bracket, Rounds, Results, Predictions, Betting,
                    Autocomplete, FinalChecks, ipy.Extension):
    final = groups.final
    bracket_group = groups.bracket_group
    round_group = groups.round_group
    match_group = groups.match_group
    predictions_group = groups.predictions_group
    betting_group = groups.betting_group

def setup(bot):
    FinalCommands(bot)

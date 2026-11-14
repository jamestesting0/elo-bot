from unittest.mock import AsyncMock, patch

from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import FinalBettingMarket


def empty_final_storage(case):
    for model in (EloFinal, FinalMatchResult, FinalBettingMarket):
        case.enterContext(patch.object(model, "find_one", new=AsyncMock(return_value=None)))

from unittest.mock import AsyncMock, patch

from elobot.db import EloFinal
from elobot.db.final_models import FinalMatchResult
from elobot.db.token_models import FinalBettingMarket


def empty_final_storage(case):
    for model in (EloFinal, FinalMatchResult, FinalBettingMarket):
        case.enterContext(patch.object(model, "find_one", new=AsyncMock(return_value=None)))

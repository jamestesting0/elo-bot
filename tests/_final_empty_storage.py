from unittest.mock import AsyncMock, patch

from database_models import EloFinal
from database_models.final_models import FinalMatchResult
from database_models.token_models import FinalBettingMarket


def empty_final_storage(case):
    for model in (EloFinal, FinalMatchResult, FinalBettingMarket):
        case.enterContext(patch.object(model, "find_one", new=AsyncMock(return_value=None)))

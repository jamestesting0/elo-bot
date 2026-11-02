import unittest




from tests._result_finalization_fixture import ResetFinalizationStateMixin


class TestResetFinalizationConsistency(ResetFinalizationStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_reset_waits_for_the_finalizers_second_player_write(self) -> None:
        await self.exercise("finalizer_player2")

    async def test_reset_waits_for_the_finalizers_record_insert(self) -> None:
        await self.exercise("finalizer_insert")

    async def test_reset_waits_for_a_suspended_resubmit(self) -> None:
        await self.exercise("resubmit_player2")

    async def test_reset_waits_for_a_suspended_manual_result(self) -> None:
        await self.exercise("manual_player2")

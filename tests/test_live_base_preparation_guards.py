import unittest
from unittest.mock import AsyncMock

from cogs.base_store import live_service
from tests._live_base_fixture import CLAN, LiveBaseFixture


class TestLiveBasePreparationGuards(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = LiveBaseFixture()
        await self.f.setup(self)

    async def store(self):
        await live_service.store_self_live_bases(self.f.runtime, self.f.ctx, await self.f.find({}))

    async def test_legacy_bases_without_expiration_can_be_stored(self):
        for participant in self.f.document['participants']:
            for base in participant['bases']:
                del base['expiration_date']
        self.f.jobs(['succeeded', 'succeeded'])
        await self.store()
        self.assertEqual(len(self.f.http.create_payloads), 2)
        stores = [p['base_store'] for p in self.f.document['participants']]
        self.assertTrue(all(s['ready'] for s in stores))
        self.assertEqual(self.f.messages[stores[0]['message_id']].embeds[0].title, '**Bases Stored**')

    async def test_reordered_base_fields_can_be_stored(self):
        for participant in self.f.document['participants']:
            participant['bases'] = [dict(reversed(list(base.items()))) for base in participant['bases']]
        self.f.jobs(['succeeded', 'succeeded'])
        await self.store()
        self.assertEqual(len(self.f.http.create_payloads), 2)
        self.assertTrue(all(p['base_store']['ready'] for p in self.f.document['participants']))

    async def test_slot_claimed_during_preparation_reports_slot_contention(self):
        self.f.runtime._base_store_sync_slots = AsyncMock(side_effect=[
            {CLAN: ['Slot 1', 'Slot 2']}, {CLAN: ['Slot 2']},
        ])
        await self.store()
        self.assertIn('no longer has both base slots free', self.f.ctx.send.await_args.args[0])
        self.assertFalse(self.f.writes)
        self.assertFalse(self.f.http.create_payloads)

    async def test_missing_base_reports_the_player_and_district_without_claiming_slots(self):
        self.f.document['participants'][0]['bases'][0] = None
        await self.store()
        message = self.f.ctx.send.await_args.args[0]
        self.assertIn('No eligible base', message)
        self.assertIn('<@222>', message)
        self.assertIn('Barbarian Camp', message)
        self.assertNotIn('changed', message)
        self.assertFalse(self.f.writes)
        self.assertFalse(self.f.http.create_payloads)

    async def test_changed_base_during_claim_is_rejected(self):
        original = self.f.write.side_effect
        async def update(query, operation):
            self.f.document['participants'][0]['bases'][0]['link'] = 'changed'
            return await original(query, operation)
        self.f.write.side_effect = update
        await self.store()
        self.assertFalse(self.f.http.create_payloads)
        self.assertTrue(all(p['base_store'] is None for p in self.f.document['participants']))
        self.assertIn('changed', self.f.ctx.send.await_args.args[0])

    async def test_changed_base_after_first_job_prevents_second_and_success_controls(self):
        original = self.f.write.side_effect
        async def update(query, operation):
            result = await original(query, operation)
            if 'participants.0.base_store.ready' in operation['$set']:
                self.f.document['participants'][0]['bases'][0]['link'] = 'changed'
            return result
        self.f.write.side_effect = update
        self.f.jobs(['succeeded'])
        await self.store()
        self.assertEqual(len(self.f.http.create_payloads), 1)
        stores = [p['base_store'] for p in self.f.document['participants']]
        self.assertEqual([s['ready'] for s in stores], [True, False])
        message = self.f.messages[stores[0]['message_id']]
        self.assertEqual(message.embeds[0].title, '**Base Storage Incomplete**')
        self.assertEqual([b.label for b in message.components[0].components], ['Undo'])

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from elobot.features.matches import base_selection
from elobot.db import BaseLink, Match, Participant
from tests._patch_sites import patch_sites
import elobot.features.matches.lifecycle as lifecycle_module
import elobot.extensions.match.base_store as match_base_store_module


DISTRICTS = ['Barbarian Camp', 'Balloon Lagoon', 'Dragon Cliffs']


class TestBaseQualitySelection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(Match, '_document_settings', DocumentSettings(
            name='matches', use_state_management=True,
        )))
        self.document = Match.model_construct(
            id=PydanticObjectId(), token='QUALITY', matched_time=1,
            primary_base_quality='Excellent', secondary_base_quality='Great',
            final_districts=DISTRICTS,
            participants=[Participant(user_id='111'), Participant(user_id='222')],
        ).model_dump()
        self.layouts = [SimpleNamespace(
            link=f'{district}/{quality}', image_url='image', district=district,
            quality=quality, records=[], builder_id=None, expiration_date=9999999999,
        ) for district in DISTRICTS for quality in ('Excellent', 'Great')]
        self.writes = []
        self.collection = MagicMock(update_one=AsyncMock(side_effect=self.update))
        self.enterContext(patch.object(Match, 'get_pymongo_collection', return_value=self.collection))
        self.enterContext(patch.object(Match, 'find_one', new=AsyncMock(side_effect=self.load)))
        async def save_match(match, *args, **kwargs):
            return await self.save(match, *args, **kwargs)
        self.enterContext(patch.object(Match, 'save_changes', new=save_match))
        self.match_model = self.enterContext(patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')))
        self.match_model.find_one = AsyncMock(side_effect=self.load)
        self.match_model.find.return_value.to_list = AsyncMock(side_effect=self.all_matches)
        self.base_model = self.enterContext(patch_sites(('elobot.extensions.match.bases.BaseLayout', 'elobot.features.matches.attack_bases.BaseLayout', 'elobot.features.results.recording.BaseLayout')))
        self.base_model.district.__eq__.side_effect = lambda v: {'district': v}
        self.base_model.quality.__eq__.side_effect = lambda v: {'quality': v}
        self.base_model.builder_id.__ne__.side_effect = lambda v: {'builder_id': {'$ne': v}}
        self.base_model.expiration_date.__gt__.side_effect = lambda v: {'expiration_date': {'$gt': v}}
        self.base_model.find.side_effect = self.find_layouts
        self.enterContext(patch.object(base_selection, 'BaseLayout', self.base_model))
        self.enterContext(patch('elobot.features.matches.attack_bases.NotIn', side_effect=lambda _, links: {'link': {'$nin': links}}))
        self.enterContext(patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))).find_one = AsyncMock(return_value=None)
        self.requests = []

    def get(self, key):
        value = self.document
        for part in ('id' if key == '_id' else key).split('.'):
            value = value[int(part)] if isinstance(value, list) else value.get(part)
        return value

    async def load(self, *args, **kwargs):
        match = Match.model_validate(deepcopy(self.document))
        match._save_state()
        return match

    async def all_matches(self):
        return [await self.load()]

    async def save(self, match, *args, **kwargs):
        for key, value in match.get_changes().items():
            self.set_value(key, value)
        match._save_state()
        return match

    def set_value(self, key, value):
        parent = self.document
        parts = key.split('.')
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = deepcopy(value)

    async def update(self, query, changes):
        self.writes.append(deepcopy(changes))
        if any(self.get(key) != value for key, value in query.items()):
            return SimpleNamespace(matched_count=0)
        for key, value in changes['$set'].items():
            self.set_value(key, value)
        return SimpleNamespace(matched_count=1)

    def find_layouts(self, *queries):
        self.requests.append(queries)
        layouts = self.layouts
        for query in queries:
            for field, condition in query.items():
                if isinstance(condition, dict):
                    if '$in' in condition:
                        layouts = [b for b in layouts if getattr(b, field) in condition['$in']]
                    if '$nin' in condition:
                        layouts = [b for b in layouts if getattr(b, field) not in condition['$nin']]
                    if '$ne' in condition:
                        layouts = [b for b in layouts if getattr(b, field) != condition['$ne']]
                    if '$gt' in condition:
                        layouts = [b for b in layouts if getattr(b, field) > condition['$gt']]
                else:
                    layouts = [b for b in layouts if getattr(b, field) == condition]
        return SimpleNamespace(to_list=AsyncMock(return_value=layouts))

    async def choose(self):
        return await match_base_store_module.choose_bases(
            'QUALITY', SimpleNamespace(id=111), SimpleNamespace(id=222), True,
        )

    async def test_retry_does_not_upgrade_missing_secondary_base(self):
        self.layouts = [b for b in self.layouts if b.link != 'Barbarian Camp/Great']
        with patch.object(lifecycle_module.random, 'randint', side_effect=[0, 1]):
            first = await self.choose()
            second = await self.choose()
        self.assertIsNone(first.participants[1].bases[0])
        self.assertIsNone(second.participants[1].bases[0])
        self.assertEqual([b.link for b in second.participants[1].bases[1:]],
                         ['Balloon Lagoon/Excellent', 'Dragon Cliffs/Excellent'])

    async def test_retry_does_not_downgrade_missing_primary_base(self):
        self.layouts = [b for b in self.layouts if b.link != 'Balloon Lagoon/Excellent']
        with patch.object(lifecycle_module.random, 'randint', side_effect=[0, 1]) as roll:
            await self.choose()
            second = await self.choose()
        self.assertIsNone(second.participants[1].bases[1])
        self.assertEqual(second.participants[1].bases[0].link, 'Barbarian Camp/Great')
        self.assertEqual(roll.call_count, 1)

    async def test_missing_secondary_is_filled_only_when_its_quality_becomes_available(self):
        missing = next(b for b in self.layouts if b.link == 'Barbarian Camp/Great')
        self.layouts.remove(missing)
        with patch.object(lifecycle_module.random, 'randint', return_value=0):
            first = await self.choose()
        self.assertEqual(first.participants[1].secondary_base_index, 0)
        self.layouts.append(missing)
        with patch.object(lifecycle_module.random, 'randint', side_effect=AssertionError('rerolled')):
            result = await self.choose()
        self.assertEqual([b.link for b in result.participants[1].bases], [
            'Barbarian Camp/Great', 'Balloon Lagoon/Excellent', 'Dragon Cliffs/Excellent',
        ])

    async def test_failed_assignment_write_selects_no_bases(self):
        from pymongo.errors import PyMongoError
        self.collection.update_one.side_effect = PyMongoError('offline')
        with self.assertRaises(PyMongoError):
            await self.choose()
        self.assertFalse(self.requests)
        self.assertEqual(self.document['participants'][1]['bases'], [None, None, None])

    async def test_stale_selector_uses_assignment_claimed_by_another_selector(self):
        first = await self.load()
        stale = await self.load()
        with patch.object(base_selection.random, 'randint', side_effect=[0, 1]):
            assigned = await base_selection.ensure_secondary_base_index(first, 1, 'Excellent', 'Great')
            competing = await base_selection.ensure_secondary_base_index(stale, 1, 'Excellent', 'Great')
        self.assertEqual(assigned.participants[1].secondary_base_index, 0)
        self.assertEqual(competing.participants[1].secondary_base_index, 0)
        self.assertEqual(self.document['participants'][1]['secondary_base_index'], 0)
        self.assertIsNone(self.document['participants'][0]['secondary_base_index'])
        self.assertTrue(all(set(write['$set']) == {'participants.1.secondary_base_index'} for write in self.writes))

    async def test_legacy_partial_primary_bases_keep_missing_secondary_requirement(self):
        participant = self.document['participants'][1]
        del participant['secondary_base_index']
        participant['bases'][1:] = [
            BaseLink(link=f'{district}/Excellent', image='image').model_dump() for district in DISTRICTS[1:]
        ]
        self.layouts = [b for b in self.layouts if b.link != 'Barbarian Camp/Great']
        result = await self.choose()
        self.assertEqual(result.participants[1].secondary_base_index, 0)
        self.assertIsNone(result.participants[1].bases[0])
        self.assertEqual(result.participants[1].bases[1].link, 'Balloon Lagoon/Excellent')

    async def test_legacy_secondary_base_keeps_missing_bases_primary(self):
        self.document['participants'][1]['bases'][0] = BaseLink(
            link='Barbarian Camp/Great', image='image',
        ).model_dump()
        result = await self.choose()
        self.assertEqual(result.participants[1].secondary_base_index, 0)
        self.assertEqual([b.link for b in result.participants[1].bases[1:]],
                         ['Balloon Lagoon/Excellent', 'Dragon Cliffs/Excellent'])

    async def test_presets_and_custom_submissions_are_not_replaced_or_used_as_generated_quality(self):
        self.document['participants'][1]['bases'][:2] = [
            BaseLink(link='Barbarian Camp/Great', image='preset', expiration_date=9999999999).model_dump(),
            BaseLink(link='custom submission', image='custom').model_dump(),
        ]
        with patch.object(base_selection.random, 'randint', return_value=2):
            result = await self.choose()
        self.assertEqual(result.participants[1].secondary_base_index, 2)
        self.assertEqual([b.link for b in result.participants[1].bases],
                         ['Barbarian Camp/Great', 'custom submission', 'Dragon Cliffs/Great'])
        self.assertEqual(result.participants[1].bases[0].image, 'preset')
        self.assertEqual(result.participants[1].bases[1].image, 'custom')

    async def test_complete_existing_selection_is_untouched(self):
        originals = [BaseLink(link=f'{d}/Excellent', image='image').model_dump() for d in DISTRICTS]
        self.document['participants'][1]['bases'] = deepcopy(originals)
        with patch.object(base_selection.random, 'randint', side_effect=AssertionError('rerolled')):
            result = await self.choose()
        self.assertEqual([b.model_dump() for b in result.participants[1].bases], originals)
        self.assertFalse(self.writes)
        self.assertFalse(self.requests)

    def test_secondary_slot_is_optional_for_old_documents_and_bounded(self):
        from pydantic import ValidationError
        self.assertIsNone(Participant(user_id='111').secondary_base_index)
        for value in (-1, 3):
            with self.assertRaises(ValidationError):
                Participant(user_id='111', secondary_base_index=value)

    async def test_legacy_inference_applies_wizard_and_capital_quality_mappings(self):
        self.document['primary_base_quality'] = 'Great'
        self.document['secondary_base_quality'] = 'Good'
        self.document['final_districts'] = ['Barbarian Camp', 'Wizard Valley', 'Capital Peak']
        self.document['participants'][1]['bases'] = [
            None,
            BaseLink(link='wizard', image='image').model_dump(),
            BaseLink(link='capital', image='image').model_dump(),
        ]
        self.layouts = [SimpleNamespace(link='wizard', quality='Great'), SimpleNamespace(link='capital', quality='Good')]
        result = await base_selection.ensure_secondary_base_index(await self.load(), 1, 'Great', 'Good')
        self.assertEqual(result.participants[1].secondary_base_index, 1)

    async def test_inconsistent_legacy_generated_mix_does_not_silently_assign_another_quality(self):
        self.document['participants'][1]['bases'][:2] = [
            BaseLink(link=f'{district}/Great', image='image').model_dump() for district in DISTRICTS[:2]
        ]
        with self.assertRaises(ValueError):
            await self.choose()
        self.assertFalse(self.writes)
        self.assertIsNone(self.document['participants'][1]['bases'][2])

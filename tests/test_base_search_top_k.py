from __future__ import annotations

import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId

import cogs.base_commands as commands
from elobot.features.bases.fingerprints import matcher, search
from elobot.features.bases.fingerprints.worker import WorkerRequest
from elobot.db import BaseLayout
from tests import _base_search_fixture as fixture
from tests._relative_geometry_fixture import layout, points
from tests._base_fingerprint_fixture import fingerprint


class ScoreReply(TypedDict):
    scores: list[Mapping[str, str | int | float | bool | None]]


class TestSearchTopK(unittest.IsolatedAsyncioTestCase):
    context = fixture.FingerprintSearchStateMixin.context

    def setUp(self) -> None:
        fixture.FingerprintSearchStateMixin.setUp(self)
        self.enterContext(patch.object(commands, "find_matches", search.find_matches))
        self.query = layout(points(53))
        self.partial = [layout([*points(53)[:-n],
                                *[("cannon", .99 - i * .002, .79 - i * .002) for i in range(n)]])
                        for n in range(1, 6)]
        self.bases = [self.bases[0].model_copy(update={
            "id": ObjectId(), "fingerprint": fp, "link": f"https://base/{index}",
        }) for index, fp in enumerate([self.query, *self.partial])]
        self.bot.base_fingerprints.query.return_value = self.query
        self.bot.base_fingerprints.cpu = SimpleNamespace(request=AsyncMock(side_effect=self._score))
        self.collection = MagicMock()
        self.collection.count_documents = AsyncMock(side_effect=lambda _: len(self.bases))
        self.collection.find.side_effect = self._cursor
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=self.collection))

    def _cursor(self, filters) -> MagicMock:
        cursor = MagicMock()
        cursor.batch_size.return_value = cursor
        cursor.__aiter__.return_value = [base.model_dump() for base in self.bases
                                        if base.quality != "Private" and base.district == filters["district"]]
        return cursor

    def _score(self, payload) -> ScoreReply:
        request = WorkerRequest.model_validate(payload)
        score = getattr(matcher, "rank_layout", matcher.score_layout) if payload.get("ranking") else matcher.score_layout
        return {"scores": [score(request.query, candidate, request.fuzzy).model_dump(mode="json")
                           for candidate in request.candidates]}

    async def test_zero_threshold_top_five_returns_partial_candidates_instead_of_only_exact_match(self) -> None:
        found, indexed, total = await search.find_matches(
            self.bot.base_fingerprints, self.query, {"district": "Barbarian Camp"},
            top=5, minimum=0, fuzzy=False,
        )
        self.assertEqual(len(found), 5, "A zero threshold must not hide all four partial alignments")
        self.assertEqual((indexed, total), (6, 6))
        self.assertEqual([score.similarity for score in found], [100, 98.1, 96.2, 94.3, 92.5])
        self.assertTrue(self.bot.base_fingerprints.cpu.request.await_args.args[0]["ranking"])

    async def test_slash_callback_pages_all_five_results_and_marks_estimated_scores(self) -> None:
        ctx = self.context()
        await self.cog.base_find.callback.func(
            self.cog, ctx, "Barbarian Camp", SimpleNamespace(url="synthetic"), top=5, min_similarity=0,
        )
        self.assertEqual(ctx.send.await_args.kwargs["embeds"][0].title, "**Similar Base 1/5**")
        session = next(iter(self.cog._similarity.sessions.values()))
        ctx.message.id = 777
        ctx.custom_id = f"base_similar|{session.token}|next"
        for page in range(2, 6):
            await self.cog.similar_page.callback.func(self.cog, ctx)
            payload = ctx.edit_origin.await_args.kwargs
            self.assertEqual(payload["embeds"][0].title, f"**Similar Base {page}/5**")
            self.assertIn("estimate", payload["embeds"][0].fields[0].value)
            self.assertFalse(any("not reliable" in field.value or "Compare may be unavailable" in field.value
                                 for field in payload["embeds"][0].fields))
        self.assertTrue(payload["components"][0].components[-1].disabled)
        self.assertIsNone(self.pending.resolution)

    async def test_review_button_uses_shared_ranking_and_returns_high_scoring_partial_candidates(self) -> None:
        ctx = self.context()
        await self.cog.find_similar_submission.callback.func(self.cog, ctx)
        self.assertEqual(ctx.send.await_args.kwargs["embeds"][0].title, "**Similar Base 1/6**")
        self.assertIsNone(self.pending.resolution)
        ctx.message.edit.assert_not_awaited()

    async def test_positive_threshold_still_filters_estimates_and_does_not_pad_to_top_k(self) -> None:
        found, _, _ = await search.find_matches(
            self.bot.base_fingerprints, self.query, {"district": "Barbarian Camp"},
            top=5, minimum=98, fuzzy=False,
        )
        self.assertEqual([score.similarity for score in found], [100, 98.1])

    async def test_global_top_five_spans_multiple_worker_batches(self) -> None:
        self.bases = [self.bases[0].model_copy(update={"id": ObjectId(), "fingerprint": layout(points(53, seed=seed))})
                      for seed in range(25)] + [self.bases[0]]
        found, indexed, total = await search.find_matches(
            self.bot.base_fingerprints, self.query, {"district": "Barbarian Camp"},
            top=5, minimum=0, fuzzy=False,
        )
        self.assertEqual(len(found), 5)
        self.assertEqual((indexed, total), (26, 26))
        self.assertEqual(found[0].base_id, str(self.bases[-1].id))
        self.assertEqual(found[0].similarity, 100)
        self.assertEqual([len(call.args[0]["candidates"])
                          for call in self.bot.base_fingerprints.cpu.request.await_args_list], [20, 6])

    async def test_sparse_query_and_private_or_stale_candidates_remain_excluded(self) -> None:
        self.bases[1].quality = "Private"
        self.bases[2].image_revision = 1
        found, indexed, _ = await search.find_matches(
            self.bot.base_fingerprints, self.query, {"district": "Barbarian Camp"},
            top=5, minimum=0, fuzzy=False,
        )
        self.assertEqual(len(found), 4)
        self.assertEqual(indexed, 4)
        self.assertNotIn(str(self.bases[1].id), [score.base_id for score in found])
        self.assertNotIn(str(self.bases[2].id), [score.base_id for score in found])
        sparse, _, _ = await search.find_matches(
            self.bot.base_fingerprints, layout(points(12)), {"district": "Barbarian Camp"},
            top=5, minimum=0, fuzzy=False,
        )
        self.assertEqual(sparse, [])

    def test_ranking_does_not_relax_strict_comparison_alignment(self) -> None:
        strict = matcher.score_layout(self.query, self.partial[0], False)
        self.assertEqual(strict.status, "insufficient")
        self.assertIsNone(strict.similarity)
        ranking = getattr(matcher, "rank_layout", matcher.score_layout)(self.query, self.partial[0], False)
        self.assertEqual(ranking.matched, 52)
        self.assertIsNone(ranking.similarity)
        self.assertGreater(ranking.verified, 0)

    async def test_empty_nonhall_counts_cannot_be_turned_into_a_score(self) -> None:
        original = fingerprint()
        query = original.model_copy(update={"buildings": original.buildings[:1], "total_detections": 1})
        self.bases = [self.bases[0].model_copy(update={"fingerprint": query})]
        self.bot.base_fingerprints.cpu.request.side_effect = None
        self.bot.base_fingerprints.cpu.request.return_value = {"scores": [{
            "status": "insufficient", "total": 0, "verified": 1,
        }]}
        found, indexed, _ = await search.find_matches(
            self.bot.base_fingerprints, query, {"district": "Barbarian Camp"}, top=5, minimum=0, fuzzy=False,
        )
        self.assertEqual(indexed, 1)
        self.assertEqual(found, [])


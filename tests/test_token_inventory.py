import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.core.token_items import TokenItem
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.tokens.item_view import item_panel


class TestTokenInventory(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_filters_unowned_inactive_items_and_repaginates_after_items_disappear(self):
        wallet = TokenWallet.model_construct(id="1", user_id="1", inventory={
            TokenItem.PRIORITY_PRO: 0, TokenItem.PRIORITY_ULTRA: 2, TokenItem.DISTRICT: 0,
            TokenItem.CHALLENGE: 0, TokenItem.PROTECTION: 0, TokenItem.EXP: 4,
        })
        uses = [
            TokenItemUse.model_construct(id="district-head", wallet_id="1", item=TokenItem.DISTRICT,
                                         slot="district", state="reserved"),
            TokenItemUse.model_construct(id="district-next", wallet_id="1", item=TokenItem.DISTRICT,
                                         slot="district", state="queued"),
            TokenItemUse.model_construct(id="protection", wallet_id="1", item=TokenItem.PROTECTION,
                                         slot="protection", state="armed"),
        ]
        query = MagicMock(to_list=AsyncMock(return_value=uses))
        query.sort.return_value = query
        with patch("elo_bot.features.tokens.item_view.TokenItemUse.find", return_value=query):
            first, controls = await item_panel(wallet, "inventory", 0)
            second, _ = await item_panel(wallet, "inventory", 1)
            self.assertEqual([field.name for field in first.fields],
                             ["**Queue Priority Ultra**", "**District Choice**", "**Rank Protection**"])
            self.assertEqual([field.name for field in second.fields], ["**EXP Potion**"])
            self.assertEqual(controls[-1].components[1].label, "1/2")
            self.assertFalse(controls[-1].components[-1].disabled)

            query.to_list.return_value = []
            wallet.inventory[TokenItem.PRIORITY_ULTRA] = 0
            remaining, controls = await item_panel(wallet, "inventory", 1)
            self.assertEqual([field.name for field in remaining.fields], ["**EXP Potion**"])
            self.assertEqual(controls[-1].components[1].label, "1/1")
            self.assertTrue(controls[-1].components[0].disabled)
            self.assertTrue(controls[-1].components[-1].disabled)

            shop, _ = await item_panel(wallet, "shop", 0)
            self.assertTrue(any("Queue Priority Pro" in field.name for field in shop.fields))
            wallet.inventory[TokenItem.EXP] = 0
            empty, controls = await item_panel(wallet, "inventory", 0)
            self.assertEqual(empty.fields, [])
            self.assertEqual(controls, [])

import logging
import unittest

from elo_bot.app.client import DiscordApiRouteFilter


class TestDiscordClientLogging(unittest.TestCase):
    def test_webhook_route_logs_redact_the_token_and_keep_the_failure_visible(self):
        logger = logging.Logger("discord-webhook-log-test")
        logger.addFilter(DiscordApiRouteFilter())
        with self.assertLogs(logger, "WARNING") as captured:
            logger.warning("POST /webhooks/43/%s Received 504", "private-test-token")
            logger.warning("DELETE /webhooks/43/%s/messages/55 Received 502", "private-test-token")
        self.assertEqual(len(captured.output), 2)
        for message in captured.output:
            self.assertNotIn("private-test-token", message)
            self.assertIn("/webhooks/43/[redacted]", message)

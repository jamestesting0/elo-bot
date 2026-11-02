import unittest

from pymongo import ASCENDING, DESCENDING

from database_models import auditlog_models, base_submission_models, giveaway_models, system_models


class TestDatabaseAuxiliaryModels(unittest.TestCase):
    def test_base_submission_model_contracts(self) -> None:
        self.assertEqual(base_submission_models.PendingBaseSubmission.Settings.name, "pending_base_submissions")
        self.assertEqual(base_submission_models.PendingBaseSubmission.model_fields["id"].alias, "_id")

    def test_system_model_contracts(self) -> None:
        self.assertEqual(system_models.ServerState.Settings.name, "server_state")
        self.assertEqual(system_models.OverrideCode.Settings.name, "override_codes")
        self.assertEqual(system_models.OverrideCode.Settings.indexes[0].document["key"], {"code": ASCENDING})
        self.assertEqual(system_models.OverrideCode.Settings.indexes[1].document["key"], {"expires_at": ASCENDING})

    def test_auditlog_model_contracts(self) -> None:
        self.assertEqual(auditlog_models.AuditLogEntry.Settings.name, "audit_log_entries")
        self.assertEqual(auditlog_models.AuditLogEntry.Settings.indexes[0].document["key"], {"recorded_at_ns": DESCENDING})

    def test_giveaway_model_contracts(self) -> None:
        self.assertEqual(giveaway_models.Giveaway.Settings.name, "giveaways")
        self.assertEqual(
            giveaway_models.Giveaway.Settings.indexes[0].document["key"],
            {"ended": ASCENDING, "end_timestamp": ASCENDING},
        )
        self.assertEqual(giveaway_models.Giveaway.Settings.indexes[1].document["key"], {"end_timestamp": ASCENDING})


if __name__ == "__main__":
    unittest.main()

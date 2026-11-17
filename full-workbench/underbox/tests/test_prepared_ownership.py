import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve


class PreparedOwnershipTests(unittest.TestCase):
    def tearDown(self):
        serve.PREPARED.clear()

    def test_creation_binds_owner_and_execution_rejects_other_user(self):
        token = serve._remember_prepared({"prepared": object()}, owner_id="synthetic-a")
        self.assertEqual(serve.prepared_entry(token, "synthetic-a")["owner_id"], "synthetic-a")
        with self.assertRaises(LookupError):
            serve.prepared_entry(token, "synthetic-b", consume=True)

    def test_ttl_and_parallel_consume_are_checked(self):
        token = serve._remember_prepared({}, owner_id="synthetic-a")
        serve.prepared_entry(token, "synthetic-a", consume=True)
        with self.assertRaises(LookupError):
            serve.prepared_entry(token, "synthetic-a", consume=True)
        serve.PREPARED[token]["running"] = False
        with patch.object(serve.time, "time", return_value=serve.PREPARED[token]["expires_at"] + 1):
            with self.assertRaises(LookupError):
                serve.prepared_entry(token, "synthetic-a")

    def test_one_user_does_not_evict_another_user(self):
        token = serve._remember_prepared({}, owner_id="synthetic-b")
        for _ in range(10):
            serve._remember_prepared({}, owner_id="synthetic-a")
        self.assertIn(token, serve.PREPARED)

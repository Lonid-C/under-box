"""Monetary opt-out keeps accounting and finite search safeguards; no network."""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app.cost_budget import CostBudget,BudgetLimit,bind,search_allowed,alternate_allowed,reserve_model,settle_model


class DisabledBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'UNDERBOX_ICT_COST_BUDGET':'0',
            'UNDERBOX_DB':str(Path(self.temp.name)/'users.db')})
        env.start();self.addCleanup(env.stop)

    def test_calls_continue_past_four_yuan_without_disabling_accounting(self):
        ledger=CostBudget()
        with bind(ledger):
            for _ in range(20):ledger.reserve(.3,'synthetic_search',search=True)
            self.assertTrue(search_allowed());self.assertTrue(alternate_allowed())
        self.assertGreater(ledger.spent,4);self.assertEqual(ledger.search_calls,20)
        snapshot=ledger.snapshot()
        self.assertFalse(snapshot['enforced']);self.assertFalse(snapshot['stopped'])
        for key in ('limitCny','targetCny','remainingCny'):self.assertIsNone(snapshot[key])
        json.dumps(snapshot,allow_nan=False)

    def test_settled_usage_above_four_yuan_does_not_stop_followup_calls(self):
        ledger=CostBudget();ticket=ledger.reserve(.1,'synthetic_model')
        ledger.settle(ticket,5);ledger.reserve(.1,'synthetic_model')
        self.assertFalse(ledger.stopped);self.assertAlmostEqual(ledger.spent,5.6)

    def test_persisted_accounting_remains_unlimited_when_reloaded(self):
        ledger=CostBudget();ledger.reserve(7,'synthetic_model')
        restored=CostBudget.load(ledger.id)
        self.assertEqual(restored.id,ledger.id);self.assertEqual(restored.spent,ledger.spent)
        self.assertFalse(restored.enforced);restored.reserve(1,'synthetic_model')
        self.assertFalse(restored.stopped)

    def test_old_monetary_stop_is_cleared_without_resetting_usage(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_COST_BUDGET':'1'}):
            ledger=CostBudget();ledger.reserve(3.5,'prior')
            self.assertRaises(BudgetLimit,ledger.reserve,.1,'synthetic_model')
        restored=CostBudget.load(ledger.id)
        self.assertFalse(restored.stopped);self.assertEqual(restored.spent,4)
        restored.reserve(1,'synthetic_model');self.assertEqual(restored.spent,5)

    def test_independent_search_safety_stop_is_still_enforced_and_restored(self):
        ledger=CostBudget();ledger.search_limit=2
        for _ in range(2):ledger.reserve(.05,'synthetic_search',search=True)
        self.assertRaises(BudgetLimit,ledger.reserve,.05,'synthetic_search',search=True)
        restored=CostBudget.load(ledger.id)
        self.assertTrue(restored.stopped);self.assertEqual(restored.search_calls,2)
        with bind(restored):self.assertFalse(search_allowed())

    def test_unknown_price_is_labelled_and_does_not_block_opted_out_calls(self):
        ledger=CostBudget();body={'model':'synthetic-unknown'}
        with bind(ledger):
            ticket=reserve_model('unpriced',body)
            settle_model(ticket,'unpriced',body,{'usage':{'prompt_tokens':10,'completion_tokens':10}})
        self.assertFalse(ledger.stopped);self.assertTrue(ledger.snapshot()['estimateIncomplete'])
        self.assertEqual(ledger.unpriced_requests,1)

    def test_default_policy_still_enforces_budget_when_switch_is_absent(self):
        with patch.dict(os.environ):
            os.environ.pop('UNDERBOX_ICT_COST_BUDGET',None)
            ledger=CostBudget();self.assertTrue(ledger.enforced)
            self.assertRaises(BudgetLimit,ledger.reserve,4,'synthetic_model')

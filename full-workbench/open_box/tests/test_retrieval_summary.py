import unittest
from app.coverage import summarize


class RetrievalSummaryTests(unittest.TestCase):
    def test_provider_error_is_not_no_record(self):
        row = summarize(selected_elements=["奖项=一等奖"], retrieval_status="provider_error",
                        queries_planned=2, queries_attempted=1, limitations=[{"code": "HTTP_429"}])
        self.assertFalse(row["neutralNoRecord"])
        self.assertEqual(row["retrievalStatus"], "provider_error")

    def test_normal_zero_result_is_neutral_and_zero_coverage(self):
        row = summarize(selected_elements=["奖项=一等奖"], retrieval_status="complete")
        self.assertTrue(row["neutralNoRecord"])
        self.assertEqual(row["coverageRatio"], 0)

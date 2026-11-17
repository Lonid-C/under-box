import unittest
from app.provenance import bind_page, canonical_url, locate_quote, usable_provenance
from app.schema import Evidence


class ProvenanceTests(unittest.TestCase):
    def test_whitespace_has_exact_original_offsets(self):
        body = "公告\n合成人 2024年\n一等奖。"
        start, end = locate_quote("合成人2024年一等奖", body)
        self.assertEqual(body[start:end], "合成人 2024年\n一等奖")

    def test_no_fuzzy_identity_year_role_or_award(self):
        body = "合成人2024年获一等奖，角色为成员"
        for quote in ("另一人2024年获一等奖", "合成人2025年获一等奖", "合成人2024年获二等奖", "角色为队长"):
            self.assertIsNone(locate_quote(quote, body))

    def test_omitted_fragments_not_continuous_quote(self):
        self.assertIsNone(locate_quote("合成人[…]一等奖", "合成人 合成学校 一等奖"))

    def test_ocr_is_not_verified_page_text(self):
        ev = Evidence(url="https://a.example/record", title="合成名单", publisher="合成机构",
                      source_tier="A", snippet="合成人一等奖", extraction_method="ocr")
        bind_page(ev, ev.snippet)
        self.assertFalse(usable_provenance(ev))
        self.assertEqual(ev.provenance_state, "ocr_unverified")

    def test_tracking_parameters_only_are_removed(self):
        self.assertEqual(canonical_url("https://a.example/record?utm_source=x&id=7#part"),
                         "https://a.example/record?id=7")

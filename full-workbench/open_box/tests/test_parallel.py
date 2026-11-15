"""陈述并行（app/pipeline.py）：每条陈述同时开始检索，报告顺序不变；画像与拆分同时调用。"""
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import pipeline
from app.schema import Claim
from app.strategy import ResumeProfile

RESUME = Path(__file__).resolve().parent.parent / "fixtures" / "resume_lin.md"


def _claims(n):
    return [Claim(id=f"c{i:02d}", raw_text=f"第{i}条", raw_locator=f"第{i}行", category="竞赛",
                  date_label="2024.05", date_start="2024-05-01", elements=[f"赛事=赛事{i}"],
                  entities={"org": "某大学"}) for i in range(1, n + 1)]


class _NoLLM:
    def complete_json(self, *a, **k):
        raise AssertionError("这里不该调用模型")


class ParallelClaimsTests(unittest.TestCase):
    def _run(self, n, env, collect):
        logs = []
        profile = ResumeProfile(identity="student", name="某候选人")
        with patch.dict(os.environ, env), \
             patch.object(pipeline, "ensure_resume", lambda doc, llm: None), \
             patch.object(pipeline, "derive_profile", lambda text, llm: profile), \
             patch.object(pipeline, "split_claims", lambda text, llm: _claims(n)), \
             patch.object(pipeline, "collect_for_claim", collect), \
             patch.object(pipeline, "generate_question", lambda vc, llm: f"关于 {vc.claim.id} 的问题"):
            report = pipeline.run_pipeline(RESUME, llm=_NoLLM(), searcher=object(),
                                           fetcher=object(), progress=logs.append)
        return report, logs

    def test_every_claim_starts_at_the_same_time_and_order_is_kept(self):
        n = 6
        barrier = threading.Barrier(n, timeout=5)    # 6 条都进到检索里才放行；串行会超时

        def collect(claim, name, searcher, llm, **kw):
            barrier.wait()
            time.sleep(0.01 * (n - int(claim.id[1:])))     # 后面的条目先结束，检验报告不乱序
            return [], False

        report, logs = self._run(n, {"CLAIM_WORKERS": "0"}, collect)
        self.assertEqual([v.claim.id for v in report.claims], [f"c{i:02d}" for i in range(1, n + 1)])
        self.assertEqual(report.claims[2].question, "关于 c03 的问题")
        self.assertTrue(any("6 条陈述同时开始检索" in line for line in logs), logs)
        self.assertTrue(any(line.strip().startswith("✓ (6/6)") for line in logs), logs)

    def test_claim_workers_one_keeps_the_old_sequential_flow(self):
        active, peak = [0], [0]
        lock = threading.Lock()

        def collect(claim, name, searcher, llm, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return [], False

        report, logs = self._run(4, {"CLAIM_WORKERS": "1"}, collect)
        self.assertEqual(peak[0], 1)
        self.assertTrue(any("[3-5/6] (1/4)" in line for line in logs), logs)

    def test_profile_and_split_are_requested_together(self):
        barrier = threading.Barrier(2, timeout=5)
        profile = ResumeProfile(identity="student", name="某候选人")

        def derive(text, llm):
            barrier.wait()
            return profile

        def split(text, llm):
            barrier.wait()
            return _claims(1)

        seen = []
        with patch.dict(os.environ, {"CLAIM_WORKERS": "0"}), \
             patch.object(pipeline, "ensure_resume", lambda doc, llm: None), \
             patch.object(pipeline, "derive_profile", derive), \
             patch.object(pipeline, "split_claims", split), \
             patch.object(pipeline, "collect_for_claim", lambda *a, **k: ([], False)), \
             patch.object(pipeline, "generate_question", lambda vc, llm: "问题"):
            report = pipeline.run_pipeline(RESUME, llm=_NoLLM(), searcher=object(), fetcher=object(),
                                           on_profile=seen.append)
        self.assertEqual(seen, [profile])
        self.assertEqual(len(report.claims), 1)

    def test_only_selected_claims_are_searched(self):
        searched = []
        lock = threading.Lock()

        def collect(claim, name, searcher, llm, **kw):
            with lock:
                searched.append(claim.id)
            return [], False

        profile = ResumeProfile(identity="student", name="某候选人")
        logs = []
        with patch.dict(os.environ, {"CLAIM_WORKERS": "0"}), \
             patch.object(pipeline, "ensure_resume", lambda doc, llm: None), \
             patch.object(pipeline, "derive_profile", lambda text, llm: profile), \
             patch.object(pipeline, "split_claims", lambda text, llm: _claims(5)), \
             patch.object(pipeline, "collect_for_claim", collect), \
             patch.object(pipeline, "generate_question", lambda vc, llm: "问题"):
            prepared = pipeline.prepare_resume(RESUME, llm=_NoLLM(), progress=logs.append)
            self.assertEqual(searched, [])                       # 拆分阶段不检索
            self.assertEqual([c.id for c in prepared.claims], ["c01", "c02", "c03", "c04", "c05"])
            # 勾选顺序打乱也按简历原顺序出报告
            report = pipeline.verify_prepared(prepared, claim_ids=["c04", "c02"], searcher=object(),
                                              fetcher=object(), progress=logs.append)
            with self.assertRaises(ValueError):
                pipeline.verify_prepared(prepared, claim_ids=[], searcher=object(), fetcher=object())
        self.assertEqual(sorted(searched), ["c02", "c04"])
        self.assertEqual([v.claim.id for v in report.claims], ["c02", "c04"])
        self.assertTrue(any("只核验勾选的 2 条（共 5 条）" in line for line in logs), logs)

    def test_llm_calls_are_capped(self):
        active, peak = [0], [0]
        lock = threading.Lock()

        class Slow:
            calls = 0

            def complete_json(self, *a, **k):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.03)
                with lock:
                    active[0] -= 1
                return {}

        bounded = pipeline.BoundedLLM(Slow(), 3)
        threads = [threading.Thread(target=bounded.complete_json, args=("s", "u")) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(peak[0], 3)
        self.assertEqual(bounded.calls, 0)            # 其余属性透传给原对象


if __name__ == "__main__":
    unittest.main()

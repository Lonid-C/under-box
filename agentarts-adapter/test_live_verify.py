import unittest
from pathlib import Path
from live_verify import validate_request, verify_resume_text

class DummyReport:
    def model_dump(self, mode):
        return {'claims': [], 'mode': mode}

class LiveVerifyTests(unittest.TestCase):
    def test_rejects_without_consent_and_oversized(self):
        for text, consent in [('样例', False), ('', True), ('x' * 32769, True)]:
            with self.assertRaises(ValueError):
                validate_request(text, consent)
    def test_invokes_pipeline_and_removes_temporary_file(self):
        captured = []
        def pipeline(path, **kwargs):
            captured.append(Path(path))
            self.assertEqual(path.read_text(), '虚构样例')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return DummyReport()
        result = verify_resume_text('虚构样例', True, llm=object(), searcher=object(), pipeline=pipeline)
        self.assertEqual(result['mode'], 'live')
        self.assertFalse(captured[0].exists())
    def test_cleans_up_on_failure_without_fabricating_result(self):
        captured = []
        def fail(path, **kwargs):
            captured.append(Path(path))
            raise RuntimeError('search unavailable')
        with self.assertRaises(RuntimeError):
            verify_resume_text('虚构样例', True, llm=object(), searcher=object(), pipeline=fail)
        self.assertFalse(captured[0].exists())

if __name__ == '__main__':
    unittest.main()

class SwallowedFailureTest(unittest.TestCase):
    def test_collector_cannot_turn_provider_failure_into_empty_report(self):
        from live_verify import LiveVerificationUnavailable
        class Broken:
            def complete_json(self,*a,**kw): raise RuntimeError('provider secret must not be exposed')
        def pipeline(path, llm, **kw):
            try: llm.complete_json('test','test')
            except RuntimeError: pass
            return DummyReport()
        with self.assertRaises(LiveVerificationUnavailable):
            verify_resume_text('虚构样例',True,llm=Broken(),searcher=object(),pipeline=pipeline)

"""Real Underbox pipeline, isolated from the production web app and its storage."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

class LiveVerificationUnavailable(RuntimeError):
    pass


def validate_request(resume_text, consent):
    if consent is not True:
        raise ValueError('consent_required')
    if not isinstance(resume_text, str) or not resume_text.strip():
        raise ValueError('empty_resume')
    if len(resume_text.encode('utf-8')) > 32768 or '\x00' in resume_text:
        raise ValueError('invalid_resume_size')


class Guard:
    """Count provider calls even when the original collector catches failures."""
    def __init__(self, inner, method, maximum):
        self.inner, self.method, self.maximum = inner, method, maximum
        self.calls = 0
        self.errors = 0
        self.limited = False
    def invoke(self, *args, **kwargs):
        self.calls += 1
        if self.calls > self.maximum:
            self.limited = True
            raise LiveVerificationUnavailable('provider_budget_exceeded')
        try:
            return getattr(self.inner, self.method)(*args, **kwargs)
        except Exception as exc:
            # Filtered queries are intentionally retried by the original collector.
            if type(exc).__name__ != 'SearchFiltered':
                self.errors += 1
            raise
    def complete_json(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)
    def search(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)


def verify_resume_text(resume_text, consent, *, llm=None, searcher=None, pipeline=None, progress=None):
    validate_request(resume_text, consent)
    from app.llm import build_llm, NullLLM
    from app.search import build_searcher, NullSearcher, PageFetcher
    if llm is None:
        llm = build_llm()
    if searcher is None:
        searcher = build_searcher()
    if isinstance(llm, NullLLM) or isinstance(searcher, NullSearcher):
        raise LiveVerificationUnavailable('providers_not_configured')
    # Serial execution gives these per-job budgets deterministic bounds.
    os.environ['CLAIM_WORKERS'] = '1'
    llm_guard = Guard(llm, 'complete_json', 50)
    search_guard = Guard(searcher, 'search', 30)
    fetcher = PageFetcher()
    with tempfile.TemporaryDirectory(prefix='underbox-ict-') as d:
        path = Path(d) / 'resume.txt'
        path.write_text(resume_text, encoding='utf-8')
        os.chmod(path, 0o600)
        if pipeline is not None:  # Dependency injection used by offline tests.
            report = pipeline(path, llm=llm_guard, searcher=search_guard)
        else:
            from app.pipeline import prepare_resume, verify_prepared
            from app.collect import Budget
            prepared = prepare_resume(path, llm=llm_guard, progress=progress)
            if not prepared.claims:
                raise ValueError('no_verifiable_claims')
            if len(prepared.claims) > 8:
                raise ValueError('too_many_claims_max_8')
            report = verify_prepared(prepared, searcher=search_guard, fetcher=fetcher,
                budget_factory=lambda: Budget(max_searches=5, max_page_reads=6, max_seconds=60),
                progress=progress)
        if llm_guard.errors or search_guard.errors:
            raise LiveVerificationUnavailable('provider_failed')
        if llm_guard.limited or search_guard.limited:
            raise LiveVerificationUnavailable('provider_budget_exceeded')
        result = report.model_dump(mode='json')
        # Do not return a private filesystem path or raw input risks to the model.
        result.pop('source_file', None)
        result['input_risks'] = [{'kind': getattr(x, 'kind', 'input_risk')}
                                 for x in getattr(report, 'input_risks', [])]
    result.pop('verification_workflow', None)
    # Retain evidence and provenance; remove duplicate tracing/navigation to bound output.
    for claim in result.get('claims', []):
        for name in ('education_review', 'verification_contacts', 'manual_review', 'claim_verification', 'competition_lookup'):
            claim.pop(name, None)
        trace = claim.get('verification_agent') or {}
        access = trace.get('sourceAccess') or {}
        claim['verification_agent'] = {'id':trace.get('agent',{}).get('id'), 'name':trace.get('agent',{}).get('name'), 'budget':access.get('budget'), 'operations':len(access.get('events',[]))}
        if 'claim' in claim:
            claim['claim'] = {k:v for k,v in claim['claim'].items() if k in ('id','raw_text','category','date_label','elements')}
        if claim.get('status') == 'none':
            claim['display_status'] = '本次未取得可采信证据'
        claim['needs_human'] = True
    return {'mode': 'live', 'notice': '真实服务调用；只作证据辅助核查，不作录用判断。未取得证据不代表经历不实。',
        'warnings': ['网页回读失败不等于不存在记录。'] if getattr(fetcher, 'failures', {}) else [],
        'usage': {'model_calls': llm_guard.calls, 'search_calls': search_guard.calls}, 'report': result}

# 本轮代码变更清单

修改：
- `open_box/app/schema.py`、`judge.py`、`collect.py`、`paper_identity.py`
- `open_box/app/verification_agents.py`、`data_sources.py`、`pipeline.py`
- `open_box/app/internship.py`、`search.py`、`github_audit.py`、`llm.py`
- `underbox/serve.py`、`accounts.py`、`store.py`、`index.html`
- `README.md`

新增：
- `open_box/app/provenance.py`、`coverage.py`、`safe_http.py`
- `open_box/tests/test_element_proofs.py`、`test_provenance.py`、`test_handoff_trust.py`
- `open_box/tests/test_retrieval_summary.py`、`test_safe_http.py`
- `underbox/tests/test_prepared_ownership.py`、`test_request_limits.py`
- `underbox/tests/test_review_workflow.py`、`test_chat_providers.py`
- `open_box/bench/schema.py`、`metrics.py`、`run_bench.py`、`fixtures/cases.json`、`README.md`
- `docs/optimization-v2/00-baseline.md`、`CHANGELOG.md`、`architecture.md`、`benchmark.md`
- `docs/optimization-v2/validation-report.md`、`known-limitations.md`、`demos.md`、`review-workflow.md`、`changed-files.md`
- `docs/deployment/huawei-cloud.md`、`THIRD_PARTY_NOTICES.md`、`SECURITY.md`、`PRIVACY.md`
- `outputs/regression/optimization-v2-code-only/summary.json`（明确 not_run）

数据库改动仅为 schema 定义 v4，没有运行迁移或操作任何数据库。
代码未验证，Git 工作树不存在，没有 commit/push。

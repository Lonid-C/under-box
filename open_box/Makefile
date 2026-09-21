PY ?= python3
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: demo serve test fixture llm-check search-check verify clean help

## 离线演示：不需要任何 API key
demo:
	@MODE=mock $(PY) -m app.main

## 与 demo 相同，但显式指定 uvicorn（装了 uvicorn 时等价于 uvicorn app.main:app --reload）
serve:
	@MODE=mock $(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

## 第 11 节验收
test:
	@$(PY) -m tests.test_rules

## 重新生成内置 fixture（状态由 app/judge.py 的规则算出）
fixture:
	@$(PY) fixtures/build_fixture.py

## 验证 DeepSeek key / 端点 / 模型名能不能用
llm-check:
	@$(PY) -m app.cli llm-check

## 验证搜索 key / 端点 / 引擎名
search-check:
	@$(PY) -m app.cli search-check

## 真实核验（需要 DEEPSEEK_API_KEY / SEARCH_API_KEY）
verify:
	@MODE=live $(PY) -m app.cli verify samples/resume_lin.pdf -o out/report.json

clean:
	@rm -rf out/*.json __pycache__ app/__pycache__ tests/__pycache__ .pytest_cache

help:
	@grep -B1 '^[a-z]' Makefile | grep -A1 '^##' | sed 's/^## /  /'

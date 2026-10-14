PY ?= python3
NODE ?= node

.PHONY: test
test:
	@$(PY) scripts/regression.py --node "$(NODE)"

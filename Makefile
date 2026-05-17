.PHONY: validate install-hooks

validate:
	uv run ruff check src tests
	uv run pytest -q
	uv run rpr --help >/dev/null
	uv run rpr-validate --help >/dev/null

install-hooks:
	git config core.hooksPath .githooks

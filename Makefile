PREFIX ?= $(HOME)/.local
BIN    := $(PREFIX)/bin
REPO   := $(CURDIR)

.PHONY: install uninstall test lint version

install:
	install -d $(BIN)
	ln -sfn $(REPO)/bin/claude-acct $(BIN)/claude-acct
	ln -sfn $(REPO)/bin/claude-acct $(BIN)/cca
	ln -sfn $(REPO)/bin/claude-acct $(BIN)/ccl
	@echo "已安装到 $(BIN): claude-acct, cca, ccl"
	@$(BIN)/cca plugin install --all || echo "（面板没装上：之后可手动 cca plugin install --all）"
	@python3 -c "import questionary, prompt_toolkit" 2>/dev/null || \
	  echo "提示：cca handoff 的交互选择框要 questionary/prompt_toolkit，装：python3 -m pip install 'questionary==2.1.0' 'prompt_toolkit==3.0.51'"

uninstall:
	rm -f $(BIN)/claude-acct $(BIN)/cca $(BIN)/ccl

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

lint:
	uv tool run --from ruff==0.17.1 ruff check --config pyproject.toml src bin/claude-acct tests

version:
	@cat VERSION

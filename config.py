"""Configuração restrita do protótipo. Sem segredos."""
import os
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
DB_PATH = BRIDGE_DIR / "bridge.sqlite3"

DEFAULT_CODEX_BIN = (
    "/home/ubuntu/.vscode-server/extensions/"
    "openai.chatgpt-26.1007.21434-linux-arm64/bin/linux-aarch64/codex"
)
APPROVED_CODEX_BIN = DEFAULT_CODEX_BIN
CODEX_BIN = os.environ.get("CODEX_BIN", DEFAULT_CODEX_BIN)

# Não usar o repositório do bot como diretório de trabalho nesta fase.
WORKDIR = BRIDGE_DIR
TASK_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 12_000
MAX_STDERR_BYTES = 6_000
PROCESS_TERMINATE_GRACE_SECONDS = 1.0

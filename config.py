"""Configuração restrita do protótipo. Sem segredos."""
import os
from pathlib import Path
import re

# Carrega variáveis locais do .env sem sobrescrever o ambiente.
def _load_local_env():
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.is_file():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ.setdefault(key, value)

_load_local_env()


BRIDGE_DIR = Path(__file__).resolve().parent
DB_PATH = BRIDGE_DIR / "bridge.sqlite3"

DEFAULT_CODEX_BIN = (
    "/home/ubuntu/.vscode-server/extensions/"
    "openai.chatgpt-26.1021.22027-linux-arm64/bin/linux-aarch64/codex"
)
APPROVED_CODEX_BIN = DEFAULT_CODEX_BIN
CODEX_BIN = os.environ.get("CODEX_BIN", DEFAULT_CODEX_BIN)

# Não usar o repositório do bot como diretório de trabalho nesta fase.
WORKDIR = BRIDGE_DIR
TASK_TIMEOUT_SECONDS = 120
MAX_TASK_PROMPT_BYTES = 4000
MCP_BEARER_TOKEN = os.environ.get("BRIDGE_MCP_TOKEN", "")
MCP_HOST = os.environ.get("BRIDGE_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("BRIDGE_MCP_PORT", "8765"))
# Browser origins are denied unless explicitly listed. Requests without an
# Origin header (typical for native MCP clients) remain supported.
MCP_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get(
        "BRIDGE_MCP_ALLOWED_ORIGINS",
        "http://localhost,http://127.0.0.1",
    ).split(",")
    if origin.strip()
)
MAX_STDOUT_BYTES = 12_000
MAX_STDERR_BYTES = 6_000
PROCESS_TERMINATE_GRACE_SECONDS = 1.0

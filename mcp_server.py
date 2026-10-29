"""Authenticated MCP Streamable HTTP endpoint for the Codex task queue."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from config import (
    DB_PATH,
    MCP_ALLOWED_ORIGINS,
    MCP_BEARER_TOKEN,
    MCP_HOST,
    MCP_PORT,
    MAX_TASK_PROMPT_BYTES,
)
from database import create_task, get_task, initialize


PROTOCOL_VERSION = "2025-11-25"
COMPAT_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_VERSIONS = [PROTOCOL_VERSION, COMPAT_PROTOCOL_VERSION]
MAX_REQUEST_BYTES = MAX_TASK_PROMPT_BYTES + 8192


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tools():
    return [
        {
            "name": "submit_codex_task",
            "description": (
                "Enfileira uma solicitação textual na fila existente para "
                "execução controlada pelo worker Codex. Não executa comandos "
                "do sistema; o worker deve ser iniciado manualmente."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "maxLength": MAX_TASK_PROMPT_BYTES}
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        },
        {
            "name": "get_task",
            "description": "Consulta estado e resultado de uma tarefa da bridge.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        },
    ]


def _tool_payload(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("invalid tool arguments")
    if name == "submit_codex_task":
        if set(arguments) != {"task"}:
            raise ValueError("invalid tool arguments")
        task_id = create_task(DB_PATH, arguments.get("task"))
        payload = {"task_id": task_id, "status": "pending"}
    elif name == "get_task":
        if set(arguments) != {"task_id"}:
            raise ValueError("invalid tool arguments")
        task_id = arguments.get("task_id", "")
        if (
            not isinstance(task_id, str)
            or len(task_id) != 32
            or any(char not in "0123456789abcdef" for char in task_id)
        ):
            raise ValueError("invalid task id")
        row = get_task(DB_PATH, task_id)
        payload = None if row is None else {
            key: row[key]
            for key in ("id", "status", "created_at", "updated_at", "result", "error")
        }
    else:
        raise KeyError("unknown tool")
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}


class InvalidRPCRequest(Exception):
    pass


class UnsupportedProtocolVersion(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexBridgeMCP/2"

    def log_message(self, *_args):
        pass  # Never log auth headers or request content.

    def _send_bytes(self, data, status=200, content_type="application/json"):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(data, status)

    def _empty(self, status):
        self._send_bytes(b"", status, content_type=None)

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or origin == "null"
            ):
                return False
        except ValueError:
            return False
        return origin in MCP_ALLOWED_ORIGINS

    def _authorized(self):
        expected = "Bearer " + MCP_BEARER_TOKEN
        return bool(MCP_BEARER_TOKEN) and hmac.compare_digest(
            self.headers.get("Authorization", ""), expected
        )

    @staticmethod
    def _has_media_type(header, required):
        types = {
            part.split(";", 1)[0].strip().lower()
            for part in header.split(",")
            if part.strip()
        }
        return required.issubset(types)

    def _read_request(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("unsupported transfer encoding")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("invalid content type")
        if not self._has_media_type(
            self.headers.get("Accept", ""), {"application/json", "text/event-stream"}
        ):
            raise ValueError("invalid accept header")
        raw_size = self.headers.get("Content-Length")
        try:
            size = int(raw_size) if raw_size is not None else 0
        except ValueError:
            raise ValueError("invalid content length") from None
        if size < 1 or size > MAX_REQUEST_BYTES:
            raise ValueError("invalid body size")
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("incomplete body")
        request = json.loads(data.decode("utf-8"))
        if not isinstance(request, dict):
            raise InvalidRPCRequest
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise InvalidRPCRequest
        if "params" in request and not isinstance(request["params"], dict):
            raise InvalidRPCRequest
        if "id" in request and (
            isinstance(request["id"], bool)
            or not isinstance(request["id"], (str, int, float))
            or (isinstance(request["id"], float) and not request["id"].is_integer())
        ):
            raise InvalidRPCRequest
        return request

    def _protocol_version(self, request):
        if request["method"] == "initialize":
            return None
        version = self.headers.get("MCP-Protocol-Version", COMPAT_PROTOCOL_VERSION)
        if version not in SUPPORTED_VERSIONS:
            raise UnsupportedProtocolVersion(version)
        return version

    def _dispatch(self, request):
        method = request["method"]
        params = request.get("params", {})
        request_id = request.get("id")
        has_id = "id" in request

        if method == "initialize":
            if not has_id:
                raise InvalidRPCRequest
            requested = params.get("protocolVersion")
            if not isinstance(requested, str):
                raise InvalidRPCRequest
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            client_info = params.get("clientInfo")
            capabilities = params.get("capabilities")
            if not isinstance(client_info, dict) or not isinstance(capabilities, dict):
                raise InvalidRPCRequest
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex-bridge", "version": "1.0"},
                "instructions": "Use submit_codex_task to enqueue approved text tasks and get_task to retrieve their status and result.",
            }
        elif method == "notifications/initialized" or method.startswith("notifications/"):
            if has_id:
                raise InvalidRPCRequest
            return None, 202
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            name = params.get("name")
            try:
                result = _tool_payload(name, params.get("arguments", {}))
            except KeyError:
                return _rpc_error(request_id, -32602, "Unknown tool"), 200
            except (ValueError, TypeError):
                return _rpc_error(request_id, -32602, "Invalid tool arguments"), 200
        else:
            return _rpc_error(request_id, -32601, "Method not found"), 200

        if not has_id:
            return None, 202
        return _rpc_result(request_id, result), 200

    def do_GET(self):
        self._reject_streaming_method()

    def do_DELETE(self):
        self._reject_streaming_method()

    def _reject_streaming_method(self):
        if self.path != "/mcp":
            return self._send({"error": "not found"}, 404)
        if not self._authorized():
            return self._send({"error": "unauthorized"}, 401)
        if not self._origin_allowed():
            return self._send({"error": "forbidden origin"}, 403)
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path != "/mcp":
            return self._send({"error": "not found"}, 404)
        if not self._authorized():
            return self._send({"error": "unauthorized"}, 401)
        if not self._origin_allowed():
            return self._send({"error": "forbidden origin"}, 403)

        request = {}
        try:
            request = self._read_request()
            self._protocol_version(request)
            payload, status = self._dispatch(request)
            if payload is None:
                return self._empty(status)
            self._send(payload, status)
        except UnsupportedProtocolVersion as exc:
            self._send(_rpc_error(request.get("id"), -32600, "Unsupported protocol version", {"supported": SUPPORTED_VERSIONS, "requested": str(exc)}), 400)
        except json.JSONDecodeError:
            self._send(_rpc_error(None, -32700, "Parse error"), 400)
        except InvalidRPCRequest:
            self._send(_rpc_error(request.get("id"), -32600, "Invalid request"), 400)
        except (ValueError, TypeError, UnicodeDecodeError):
            self._send(_rpc_error(request.get("id"), -32600, "Invalid request"), 400)
        except Exception:
            self._send(_rpc_error(request.get("id"), -32603, "Bridge operation failed"), 500)


def main():
    if not MCP_BEARER_TOKEN or len(MCP_BEARER_TOKEN) < 32:
        raise SystemExit("Configure BRIDGE_MCP_TOKEN com pelo menos 32 caracteres aleatórios")
    initialize(DB_PATH)
    ThreadingHTTPServer((MCP_HOST, MCP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

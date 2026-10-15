"""LAN HTTP pages, uploads, downloads, and the browser WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import urllib.parse
from pathlib import Path
from typing import Any

from websockets.frames import CLOSE, TEXT, Frame
from websockets.http11 import Request
from websockets.protocol import SEND_EOF
from websockets.server import ServerProtocol

from .config import Config
from .hub import Hub, HubError
from .store import canonical_id

log = logging.getLogger("ptopchat.http")

STATIC = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
}
HISTORY_LIMIT = 200
HEADER_LIMIT = 64 * 1024


def web_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "web",
        Path.cwd() / "web",
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("web/index.html not found")


class HttpServer:
    def __init__(self, hub: Hub, config: Config) -> None:
        self.hub = hub
        self.config = config
        self.web_dir = web_dir()

    async def serve(self) -> None:
        server = await asyncio.start_server(self.handle, self.config.http_host, self.config.http_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        log.info("http listening on %s", sockets)
        async with server:
            await server.serve_forever()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await _read_request(reader, self.config.max_file_bytes)
            if request is None:
                return
            if _is_websocket(request):
                await self._handle_ws(reader, writer, request)
                return
            status, reason, body, content_type, extra = await self._route(request)
            await _write_http(writer, status, reason, body, content_type, extra)
        except ValueError as exc:
            status, reason = (413, "Payload Too Large") if "large" in str(exc) else (400, "Bad Request")
            with contextlib.suppress(Exception):
                await _write_http(writer, status, reason, _error_body(str(exc)), "application/json; charset=utf-8", [])
        except Exception:
            log.exception("http request failed")
            with contextlib.suppress(Exception):
                await _write_http(
                    writer,
                    500,
                    "Internal Server Error",
                    _error_body("internal error"),
                    "application/json; charset=utf-8",
                    [],
                )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _route(self, request: "_Request") -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
        path = request.path
        if request.method == "GET" and path in STATIC:
            return self._static(STATIC[path])
        if request.method == "GET" and path == "/api/history":
            return self._history(request)
        if request.method == "POST" and path == "/api/upload":
            return await self._upload(request)
        if request.method == "GET" and path.startswith("/api/files/"):
            return self._download(path.removeprefix("/api/files/"))
        if request.method != "GET":
            return 405, "Method Not Allowed", b'{"error":"method not allowed"}', "application/json; charset=utf-8", []
        return 404, "Not Found", b'{"error":"not found"}', "application/json; charset=utf-8", []

    def _static(self, name: str) -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
        path = self.web_dir / name
        if not path.is_file():
            return 404, "Not Found", b"not found", "text/plain; charset=utf-8", []
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".html", ".js", ".css")):
            mime += "; charset=utf-8"
        return 200, "OK", path.read_bytes(), mime, [("Cache-Control", "no-store")]

    def _history(self, request: "_Request") -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
        limit = HISTORY_LIMIT
        raw_limit = request.query.get("limit", [str(HISTORY_LIMIT)])[0]
        try:
            limit = max(1, min(HISTORY_LIMIT, int(raw_limit)))
        except ValueError:
            limit = HISTORY_LIMIT
        body = json.dumps({"messages": self.hub.history(limit)}, ensure_ascii=False).encode("utf-8")
        return 200, "OK", body, "application/json; charset=utf-8", []

    async def _upload(self, request: "_Request") -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            return _json_error(400, "Bad Request", "expected multipart form")
        try:
            fields = _parse_multipart(request.body, content_type)
        except ValueError as exc:
            return _json_error(400, "Bad Request", str(exc))
        dest = fields.get("to")
        uploaded = fields.get("file")
        if not isinstance(dest, str) or not isinstance(uploaded, dict):
            return _json_error(400, "Bad Request", "to and file are required")
        data = uploaded["data"]
        if len(data) > self.config.max_file_bytes:
            return _json_error(413, "Payload Too Large", "file too large")
        try:
            message = await self.hub.post_file(uploaded["filename"], data, dest)
        except HubError as exc:
            code = 413 if "large" in str(exc) else 400
            reason = "Payload Too Large" if code == 413 else "Bad Request"
            return _json_error(code, reason, str(exc))
        body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
        return 201, "Created", body, "application/json; charset=utf-8", []

    def _download(self, file_id: str) -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
        try:
            message = self.hub.store.get(file_id)
        except ValueError:
            return _json_error(404, "Not Found", "not found")
        if message is None or message["kind"] != "file" or not message["file_complete"]:
            return _json_error(404, "Not Found", "not found")
        data = self.hub.store.file_path(canonical_id(file_id)).read_bytes()
        filename = message["filename"] or "file"
        return (
            200,
            "OK",
            data,
            "application/octet-stream",
            [("Content-Disposition", _content_disposition(filename))],
        )

    async def _handle_ws(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: "_Request",
    ) -> None:
        protocol = ServerProtocol(max_size=1024 * 1024)
        protocol.receive_data(request.raw)
        events = protocol.events_received()
        await _flush_ws(protocol, writer)
        ws_request = next((event for event in events if isinstance(event, Request)), None)
        if ws_request is None:
            return
        response = protocol.accept(ws_request)
        protocol.send_response(response)
        await _flush_ws(protocol, writer)
        if response.status_code != 101:
            return
        client = WsClient(protocol, writer)
        self.hub.clients.add(client)
        try:
            await client.send(
                json.dumps(
                    {
                        "type": "hello",
                        "site_name": self.config.site_name,
                        "name": self.config.name,
                        "address": self.config.address,
                        "destinations": self.hub.destinations(),
                        "peer_online": self.hub.peer_online,
                        "peer_users": self.hub.peer_users,
                        "local_online": len(self.hub.clients),
                        "history": self.hub.history(HISTORY_LIMIT),
                        "max_file_bytes": self.config.max_file_bytes,
                    },
                    ensure_ascii=False,
                )
            )
            await self.hub.note_clients_changed()
            await self._ws_loop(client, protocol, reader, writer, events)
        finally:
            self.hub.clients.discard(client)
            await self.hub.note_clients_changed()


    async def _ws_loop(
        self,
        client: "WsClient",
        protocol: ServerProtocol,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        primed: list[Any],
    ) -> None:
        pending = [event for event in primed if isinstance(event, Frame)]
        while True:
            for frame in pending:
                text = _frame_text(frame)
                if text is None:
                    if frame.opcode == CLOSE or protocol.state.name in {"CLOSING", "CLOSED"}:
                        return
                    continue
                await self._on_ws_text(client, text)
            pending = []
            data = await reader.read(65536)
            if not data:
                protocol.receive_eof()
                await _flush_ws(protocol, writer)
                return
            async with client.lock:
                protocol.receive_data(data)
                events = protocol.events_received()
                await _flush_ws(protocol, writer)
            if protocol.state.name == "CLOSED":
                return
            pending = [event for event in events if isinstance(event, Frame)]

    async def _on_ws_text(self, client: "WsClient", text: str) -> None:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict) or obj.get("type") != "chat":
            return
        try:
            await self.hub.post_chat(str(obj.get("text", "")), str(obj.get("to", "")), obj.get("ref"))
        except HubError as exc:
            await client.send(json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False))


class WsClient:
    def __init__(self, protocol: ServerProtocol, writer: asyncio.StreamWriter) -> None:
        self.protocol = protocol
        self.writer = writer
        self.lock = asyncio.Lock()

    async def send(self, text: str) -> None:
        async with self.lock:
            self.protocol.send_text(text.encode("utf-8"))
            await _flush_ws(self.protocol, self.writer)


class _Request:
    def __init__(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        raw: bytes,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.raw = raw


async def _read_request(reader: asyncio.StreamReader, max_file_bytes: int) -> _Request | None:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        buf.extend(chunk)
        if len(buf) > HEADER_LIMIT:
            raise ValueError("headers too large")
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        method, target, _version = lines[0].split(" ")
    except ValueError as exc:
        raise ValueError("bad request line") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers[name.lower()] = value.strip()
    parsed = urllib.parse.urlsplit(target)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    path = urllib.parse.unquote(parsed.path)
    upgrade = headers.get("upgrade", "").lower()
    if method == "GET" and upgrade == "websocket":
        return _Request(method, path, query, headers, b"", bytes(buf))
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise ValueError("bad content length") from exc
    overhead = 64 * 1024
    if length < 0 or length > max_file_bytes + overhead:
        raise ValueError("body too large")
    body = bytearray(rest)
    while len(body) < length:
        chunk = await reader.read(min(65536, length - len(body)))
        if not chunk:
            raise ValueError("truncated body")
        body.extend(chunk)
    return _Request(method, path, query, headers, bytes(body[:length]), bytes(buf))


def _is_websocket(request: _Request) -> bool:
    return (
        request.method == "GET"
        and request.path == "/ws"
        and request.headers.get("upgrade", "").lower() == "websocket"
    )


async def _write_http(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    body: bytes,
    content_type: str,
    extra: list[tuple[str, str]],
) -> None:
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "X-Content-Type-Options: nosniff",
    ]
    headers.extend(f"{name}: {value}" for name, value in extra)
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
    await writer.drain()


def _error_body(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


def _json_error(status: int, reason: str, message: str) -> tuple[int, str, bytes, str, list[tuple[str, str]]]:
    return status, reason, _error_body(message), "application/json; charset=utf-8", []


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\"} else "_" for ch in filename)
    quoted = urllib.parse.quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'


def _parse_multipart(body: bytes, content_type: str) -> dict[str, Any]:
    marker = "boundary="
    index = content_type.lower().find(marker)
    if index < 0:
        raise ValueError("missing multipart boundary")
    boundary = content_type[index + len(marker) :].split(";", 1)[0].strip().strip('"')
    if not boundary:
        raise ValueError("missing multipart boundary")
    delimiter = b"--" + boundary.encode("ascii", "ignore")
    parts = body.split(delimiter)
    fields: dict[str, Any] = {}
    for part in parts:
        if not part or part in {b"--", b"--\r\n"} or part.startswith(b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disposition = ""
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep and name.lower() == "content-disposition":
                disposition = value.strip()
        field_name = _disp_value(disposition, "name")
        if not field_name:
            continue
        filename = _filename_from_disposition(disposition)
        if filename is None:
            fields[field_name] = data.decode("utf-8", "replace")
        else:
            fields[field_name] = {"filename": filename, "data": data}
    return fields


def _filename_from_disposition(disposition: str) -> str | None:
    match = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;\s]+)", disposition)
    if match:
        return urllib.parse.unquote(match.group(1))
    return _disp_value(disposition, "filename")


def _disp_value(disposition: str, key: str) -> str | None:
    token = key + '="'
    start = disposition.find(token)
    if start < 0:
        return None
    start += len(token)
    end = disposition.find('"', start)
    if end < 0:
        return None
    return disposition[start:end]


def _frame_text(frame: Frame) -> str | None:
    if frame.opcode == TEXT and frame.fin:
        return bytes(frame.data).decode("utf-8", "replace")
    return None


async def _flush_ws(protocol: ServerProtocol, writer: asyncio.StreamWriter) -> None:
    for chunk in protocol.data_to_send():
        if chunk == SEND_EOF:
            if writer.can_write_eof():
                writer.write_eof()
        else:
            writer.write(chunk)
    await writer.drain()


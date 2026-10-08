"""Two host processes on localhost exchange a text message and a file."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import websockets

ROOT = Path(__file__).resolve().parents[1]


def test_pages_offer_both_languages_and_hosts_exchange_text_and_file(tmp_path):
    http_a, p2p_a = _free_port(), _free_port()
    http_b, p2p_b = _free_port(), _free_port()
    data_a = tmp_path / "a"
    data_b = tmp_path / "b"
    cfg_a = _write_config(
        tmp_path / "a.toml",
        site_name="Офис A",
        http_port=http_a,
        p2p_port=p2p_a,
        dial=False,
        peer_addr="",
        data_dir=data_a,
    )
    cfg_b = _write_config(
        tmp_path / "b.toml",
        site_name="Office B",
        http_port=http_b,
        p2p_port=p2p_b,
        dial=True,
        peer_addr=f"127.0.0.1:{p2p_a}",
        data_dir=data_b,
    )
    log_a = open(tmp_path / "a.log", "w", encoding="utf-8")
    log_b = open(tmp_path / "b.log", "w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc_a = subprocess.Popen(
        [sys.executable, "-m", "ptopchat", str(cfg_a)],
        cwd=ROOT,
        env=env,
        stdout=log_a,
        stderr=subprocess.STDOUT,
    )
    proc_b = None
    try:
        _wait_port(http_a)
        _wait_port(p2p_a)
        proc_b = subprocess.Popen(
            [sys.executable, "-m", "ptopchat", str(cfg_b)],
            cwd=ROOT,
            env=env,
            stdout=log_b,
            stderr=subprocess.STDOUT,
        )
        _wait_port(http_b)
        page = _get(f"http://127.0.0.1:{http_a}/").decode()
        script = _get(f"http://127.0.0.1:{http_a}/app.js").decode()
        assert 'id="lang-ru"' in page
        assert 'id="lang-en"' in page
        assert 'id="destination"' in page
        assert 'id="nick"' not in page
        assert "ui_lang" in script

        remote_text = "hello from A"
        local_text = "stay on A"
        remote = _send_chats(http_a, remote_text, local_text)
        _upload(http_a, remote, "note.txt", b"file-body-from-a")

        deadline = time.time() + 20
        copied = None
        while time.time() < deadline:
            history = json.loads(_get(f"http://127.0.0.1:{http_b}/api/history"))
            texts = [item for item in history["messages"] if item["text"] == remote_text]
            assert all(item["text"] != local_text for item in history["messages"])
            files = [
                item
                for item in history["messages"]
                if item["kind"] == "file" and item["file_complete"] and item["filename"] == "note.txt"
            ]
            if texts and files:
                copied = files[0]
                assert texts[0]["site"] == "Офис A"
                assert texts[0]["nick"] == "Анна"
                assert texts[0]["dest"] == remote
                break
            if proc_a.poll() is not None or proc_b.poll() is not None:
                break
            time.sleep(0.1)
        assert copied is not None, _logs(tmp_path)
        body = _get(f"http://127.0.0.1:{http_b}/api/files/{copied['id']}")
        assert body == b"file-body-from-a"
    finally:
        for proc in (proc_b, proc_a):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_a.close()
        log_b.close()


def _send_chats(port: int, remote_text: str, local_text: str) -> str:
    async def _run() -> str:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            hello = await _peer_hello(ws)
            local = hello["address"]
            remote = next(addr for addr in hello["destinations"] if addr != local)
            await ws.send(json.dumps({"type": "chat", "text": remote_text, "to": remote}))
            await ws.send(json.dumps({"type": "chat", "text": local_text, "to": local}))
            seen: set[str] = set()
            deadline = time.time() + 5
            while time.time() < deadline and {remote_text, local_text} - seen:
                payload = json.loads(await ws.recv())
                if payload.get("type") == "message":
                    seen.add(payload["message"]["text"])
                    assert payload["message"]["nick"] == "Анна"
            if {remote_text, local_text} - seen:
                raise AssertionError(f"local echo missing: {seen}")
            return remote

    import asyncio

    return asyncio.run(_run())


async def _peer_hello(ws) -> dict:
    hello = None
    deadline = time.time() + 10
    while time.time() < deadline:
        payload = json.loads(await ws.recv())
        if payload.get("type") == "hello":
            hello = payload
        elif payload.get("type") == "destinations" and hello is not None:
            hello["destinations"] = payload["addresses"]
            hello["address"] = payload["address"]
        if hello and any(addr != hello["address"] for addr in hello.get("destinations", [])):
            return hello
    raise AssertionError(f"peer destination missing: {hello}")


def _upload(port: int, dest: str, filename: str, data: bytes) -> None:
    boundary = "----ptopchat"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="to"\r\n\r\n'
        f"{dest}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 201


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def _write_config(path, site_name, http_port, p2p_port, dial, peer_addr, data_dir):
    path.write_text(
        "\n".join(
            [
                f"site_name = {json.dumps(site_name)}",
                f"name = {json.dumps('Анна' if 'Офис' in site_name else 'Ben')}",
                f"address = {json.dumps(f'127.0.0.1:{p2p_port}')}",
                'http_host = "127.0.0.1"',
                f"http_port = {http_port}",
                'p2p_host = "127.0.0.1"',
                f"p2p_port = {p2p_port}",
                f"dial = {'true' if dial else 'false'}",
                f"peer_addr = {json.dumps(peer_addr)}",
                'shared_secret = "test-secret"',
                f"data_dir = {json.dumps(str(data_dir))}",
                "max_file_bytes = 1048576",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"port {port} did not open")


def _logs(tmp_path: Path) -> str:
    parts = []
    for name in ("a.log", "b.log"):
        parts.append(f"----- {name} -----\n{(tmp_path / name).read_text(encoding='utf-8')}")
    return "\n".join(parts)

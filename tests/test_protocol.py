"""Frames and the mutual handshake, without opening a network socket."""

import asyncio
import os
import socket

import pytest

from ptopchat.protocol import (
    FrameDecoder,
    ProtocolError,
    build_hello,
    mac_equal,
    open_frame,
    parse_hello,
    proof,
    seal,
    session_key,
)
from ptopchat.p2p import handshake_dialer, handshake_listener


def test_frame_roundtrip_and_split_reads():
    secret = b"test-secret"
    dialer_nonce = os.urandom(32)
    listener_nonce = os.urandom(32)
    key = session_key(secret, dialer_nonce, listener_nonce)
    assert key == session_key(secret, dialer_nonce, listener_nonce)
    blob = seal(key, {"type": "chat", "text": "привет"}, b"\x00\x01file")
    header, payload = open_frame(key, blob)
    assert header["text"] == "привет"
    assert payload == b"\x00\x01file"

    decoder = FrameDecoder(key)
    decoder.feed(blob[:6])
    assert decoder.pop() is None
    decoder.feed(blob[6:])
    assert decoder.pop() == (header, payload)
    assert decoder.pop() is None

    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    with pytest.raises(ProtocolError):
        open_frame(key, bytes(tampered))


def test_hello_rejects_bad_banner():
    nonce = os.urandom(32)
    hello = build_hello(role="dialer", site_name="Office A", address="127.0.0.1:9700", nonce=nonce)
    parse_hello(hello, "dialer")
    hello["magic"] = "NOPE"
    with pytest.raises(ProtocolError):
        parse_hello(hello, "dialer")


def test_proofs_are_mutual_and_role_separated():
    secret = b"test-secret"
    dialer_nonce = os.urandom(32)
    listener_nonce = os.urandom(32)
    listener_mac = proof(secret, "listener", dialer_nonce, listener_nonce, "A", "B", "10.0.0.2:1", "10.0.0.1:1")
    dialer_mac = proof(secret, "dialer", dialer_nonce, listener_nonce, "A", "B", "10.0.0.2:1", "10.0.0.1:1")
    assert mac_equal(listener_mac, listener_mac)
    assert not mac_equal(listener_mac, dialer_mac)
    other = proof(b"other", "listener", dialer_nonce, listener_nonce, "A", "B", "10.0.0.2:1", "10.0.0.1:1")
    assert not mac_equal(listener_mac, other)


def test_handshake_over_socketpair():
    asyncio.run(_handshake(secret_a=b"same-secret", secret_b=b"same-secret"))


def test_handshake_rejects_wrong_secret():
    asyncio.run(_handshake_rejects(b"alpha", b"beta"))


async def _handshake_rejects(secret_a: bytes, secret_b: bytes) -> None:
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    left_reader, left_writer = await asyncio.open_connection(sock=left)
    right_reader, right_writer = await asyncio.open_connection(sock=right)
    dial = asyncio.create_task(
        handshake_dialer(left_reader, left_writer, secret_a, "Офис A", "127.0.0.1:9701")
    )
    listen = asyncio.create_task(
        handshake_listener(right_reader, right_writer, secret_b, "Office B", "127.0.0.1:9700")
    )
    try:
        done, pending = await asyncio.wait(
            {dial, listen},
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        errors = []
        for task in done:
            exc = task.exception()
            if exc is not None:
                errors.append(exc)
        assert any(isinstance(exc, ProtocolError) for exc in errors), errors
    finally:
        left_writer.close()
        right_writer.close()


async def _handshake(secret_a: bytes, secret_b: bytes) -> None:
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    left_reader, left_writer = await asyncio.open_connection(sock=left)
    right_reader, right_writer = await asyncio.open_connection(sock=right)
    try:
        dial = asyncio.create_task(
            handshake_dialer(left_reader, left_writer, secret_a, "Офис A", "127.0.0.1:9701")
        )
        listen = asyncio.create_task(
            handshake_listener(right_reader, right_writer, secret_b, "Office B", "127.0.0.1:9700")
        )
        (dialer_key, peer_site, peer_address), (listener_key, dialer_site, dialer_address) = await asyncio.gather(
            dial, listen
        )
        assert dialer_key == listener_key
        assert peer_site == "Office B"
        assert peer_address == "127.0.0.1:9700"
        assert dialer_site == "Офис A"
        assert dialer_address == "127.0.0.1:9701"
        blob = seal(dialer_key, {"type": "ping"}, b"")
        left_writer.write(blob)
        await left_writer.drain()
        data = await right_reader.readexactly(len(blob))
        header, payload = open_frame(listener_key, data)
        assert header["type"] == "ping"
        assert payload == b""
    finally:
        left_writer.close()
        right_writer.close()

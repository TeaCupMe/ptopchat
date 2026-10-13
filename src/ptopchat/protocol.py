"""Length-prefixed frames, mutual HMAC, and AES-GCM for the host-to-host link."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

MAGIC = "P2PCHAT"
VERSION = 1
NONCE_LEN = 32
GCM_NONCE_LEN = 12
CHUNK_SIZE = 64 * 1024
HISTORY_WINDOW = 500
MAX_HEADER = 64 * 1024
MAX_FRAME_BODY = 1024 * 1024
MAX_HELLO = 64 * 1024
HKDF_INFO = b"ptopchat-session-v1"


class ProtocolError(Exception):
    """The peer sent a frame or handshake this host will not accept."""


def pack_json(obj: dict[str, Any]) -> bytes:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_HELLO:
        raise ProtocolError("json message is too large")
    return struct.pack(">I", len(data)) + data


def unpack_json(blob: bytes) -> dict[str, Any]:
    if len(blob) < 4:
        raise ProtocolError("short json message")
    size = struct.unpack(">I", blob[:4])[0]
    if size > MAX_HELLO or len(blob) != 4 + size:
        raise ProtocolError("bad json message length")
    try:
        obj = json.loads(blob[4:])
    except json.JSONDecodeError as exc:
        raise ProtocolError("json message is not valid") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("json message must be an object")
    return obj


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise ProtocolError("bad base64") from exc


def build_hello(
    *,
    role: str,
    site_name: str,
    address: str,
    nonce: bytes,
    mac: bytes | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "magic": MAGIC,
        "version": VERSION,
        "role": role,
        "site_name": site_name,
        "address": address,
        "nonce": _b64(nonce),
    }
    if mac is not None:
        obj["hmac"] = _b64(mac)
    return obj


def parse_hello(obj: dict[str, Any], expected_role: str) -> tuple[str, str, bytes, bytes | None]:
    if obj.get("magic") != MAGIC or obj.get("version") != VERSION:
        raise ProtocolError("unexpected handshake banner")
    if obj.get("role") != expected_role:
        raise ProtocolError("unexpected handshake role")
    site_name = obj.get("site_name")
    if not isinstance(site_name, str) or not _valid_site_name(site_name):
        raise ProtocolError("bad site name")
    address = obj.get("address")
    if not isinstance(address, str) or not _valid_address(address):
        raise ProtocolError("bad address")
    address = address.strip()
    nonce_text = obj.get("nonce")
    if not isinstance(nonce_text, str):
        raise ProtocolError("missing nonce")
    nonce = _unb64(nonce_text)
    if len(nonce) != NONCE_LEN:
        raise ProtocolError("bad nonce length")
    mac = None
    if "hmac" in obj:
        mac_text = obj["hmac"]
        if not isinstance(mac_text, str):
            raise ProtocolError("bad hmac")
        mac = _unb64(mac_text)
    return site_name, address, nonce, mac


def _valid_site_name(name: str) -> bool:
    if not name or len(name) > 80:
        return False
    return "\x00" not in name and "\n" not in name and "\r" not in name


def _valid_address(value: str) -> bool:
    from .config import split_host_port

    try:
        host, _port = split_host_port(value.strip())
    except ValueError:
        return False
    return host not in {"0.0.0.0", "::"}


def proof(
    secret: bytes,
    role: str,
    dialer_nonce: bytes,
    listener_nonce: bytes,
    dialer_site: str,
    listener_site: str,
    dialer_address: str,
    listener_address: str,
) -> bytes:
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(role.encode("ascii"))
    mac.update(b"\0")
    mac.update(dialer_nonce)
    mac.update(listener_nonce)
    mac.update(dialer_site.encode("utf-8"))
    mac.update(b"\0")
    mac.update(listener_site.encode("utf-8"))
    mac.update(b"\0")
    mac.update(dialer_address.encode("utf-8"))
    mac.update(b"\0")
    mac.update(listener_address.encode("utf-8"))
    return mac.digest()


def mac_equal(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def session_key(secret: bytes, dialer_nonce: bytes, listener_nonce: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=dialer_nonce + listener_nonce,
        info=HKDF_INFO,
    )
    return hkdf.derive(secret)


def encode_plaintext(header: dict[str, Any], payload: bytes = b"") -> bytes:
    header_bytes = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(header_bytes) > MAX_HEADER:
        raise ProtocolError("frame header is too large")
    return struct.pack(">I", len(header_bytes)) + header_bytes + payload


def decode_plaintext(data: bytes) -> tuple[dict[str, Any], bytes]:
    if len(data) < 4:
        raise ProtocolError("short frame")
    size = struct.unpack(">I", data[:4])[0]
    if size > MAX_HEADER or 4 + size > len(data):
        raise ProtocolError("bad frame header length")
    try:
        header = json.loads(data[4 : 4 + size])
    except json.JSONDecodeError as exc:
        raise ProtocolError("frame header is not json") from exc
    if not isinstance(header, dict):
        raise ProtocolError("frame header must be an object")
    return header, data[4 + size :]


def seal(key: bytes, header: dict[str, Any], payload: bytes = b"") -> bytes:
    plaintext = encode_plaintext(header, payload)
    nonce = os.urandom(GCM_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    body = nonce + ciphertext
    if len(body) > MAX_FRAME_BODY:
        raise ProtocolError("frame is too large")
    return struct.pack(">I", len(body)) + body


def open_frame(key: bytes, blob: bytes) -> tuple[dict[str, Any], bytes]:
    if len(blob) < 4:
        raise ProtocolError("short sealed frame")
    size = struct.unpack(">I", blob[:4])[0]
    if size < GCM_NONCE_LEN + 16 or size > MAX_FRAME_BODY or len(blob) != 4 + size:
        raise ProtocolError("bad sealed frame length")
    body = blob[4:]
    nonce, ciphertext = body[:GCM_NONCE_LEN], body[GCM_NONCE_LEN:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ProtocolError("frame authentication failed") from exc
    return decode_plaintext(plaintext)


class FrameDecoder:
    """Pull complete sealed frames out of a TCP byte stream."""

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        if len(self._buf) > 8 * MAX_FRAME_BODY:
            raise ProtocolError("peer sent too much data")

    def pop(self) -> tuple[dict[str, Any], bytes] | None:
        if len(self._buf) < 4:
            return None
        size = struct.unpack(">I", self._buf[:4])[0]
        if size < GCM_NONCE_LEN + 16 or size > MAX_FRAME_BODY:
            raise ProtocolError("bad sealed frame length")
        if len(self._buf) < 4 + size:
            return None
        blob = bytes(self._buf[: 4 + size])
        del self._buf[: 4 + size]
        return open_frame(self._key, blob)

"""TOML configuration for one chat host."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    site_name: str
    name: str
    address: str
    http_host: str
    http_port: int
    p2p_host: str
    p2p_port: int
    dial: bool
    peer_addr: str
    shared_secret: str
    data_dir: Path
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    @property
    def secret_bytes(self) -> bytes:
        return self.shared_secret.encode("utf-8")


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        site_name = str(raw["site_name"]).strip()
        name = " ".join(str(raw["name"]).split())
        address = str(raw["address"]).strip()
        http_host = str(raw["http_host"]).strip()
        http_port = int(raw["http_port"])
        p2p_host = str(raw["p2p_host"]).strip()
        p2p_port = int(raw["p2p_port"])
        dial = bool(raw["dial"])
        peer_addr = str(raw.get("peer_addr", "")).strip()
        shared_secret = str(raw["shared_secret"])
        data_dir = Path(raw["data_dir"])
        max_file_bytes = int(raw.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid config {config_path}") from exc
    if not site_name or len(site_name) > 80:
        raise ValueError("site_name must be 1..80 characters")
    if not name or len(name) > 80 or "\n" in name or "\r" in name:
        raise ValueError("name must be 1..80 characters")
    address_host, _port = split_host_port(address)
    if address_host in {"0.0.0.0", "::"}:
        raise ValueError("address must be a reachable host:port")
    if not http_host or not p2p_host:
        raise ValueError("http_host and p2p_host are required")
    for port in (http_port, p2p_port):
        if port < 0 or port > 65535:
            raise ValueError("ports must be in 0..65535")
    if not shared_secret:
        raise ValueError("shared_secret is required")
    if dial and not peer_addr:
        raise ValueError("dial requires peer_addr")
    if peer_addr:
        split_host_port(peer_addr)
    if max_file_bytes < 1:
        raise ValueError("max_file_bytes must be positive")
    if not data_dir.is_absolute():
        data_dir = (config_path.parent / data_dir).resolve()
    return Config(
        site_name=site_name,
        name=name,
        address=address,
        http_host=http_host,
        http_port=http_port,
        p2p_host=p2p_host,
        p2p_port=p2p_port,
        dial=dial,
        peer_addr=peer_addr,
        shared_secret=shared_secret,
        data_dir=data_dir,
        max_file_bytes=max_file_bytes,
    )


def split_host_port(addr: str) -> tuple[str, int]:
    if addr.startswith("["):
        host, _, port_text = addr[1:].rpartition("]:")
        if not host:
            raise ValueError(f"bad peer_addr {addr}")
    else:
        host, sep, port_text = addr.rpartition(":")
        if not sep or not host:
            raise ValueError(f"bad peer_addr {addr}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"bad peer_addr {addr}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"bad peer_addr {addr}")
    return host, port

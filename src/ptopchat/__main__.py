"""Run one chat host: LAN web page plus the peer TCP link."""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import load_config
from .http_app import HttpServer
from .hub import Hub
from .p2p import PeerManager
from .store import Store


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m ptopchat CONFIG.toml", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        config = load_config(args[0])
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        return 0
    return 0


async def serve(config) -> None:
    store = Store(config.data_dir)
    hub = Hub(config, store)
    peer = PeerManager(hub, config)
    http = HttpServer(hub, config)
    try:
        await asyncio.gather(peer.serve(), http.serve())
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

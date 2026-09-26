#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service.

Entry point; judgment (:mod:`service`), storage (:mod:`store`), transport
(:mod:`httpapp`) and the coordination console (``static/index.html``) are
maintained separately.
"""
from __future__ import annotations

import argparse
import os

from httpapp import create_server
from kernel import PORT, ApiError, iso, parse_time, utcnow
from service import DroneAirspaceService
from store import Repository

__all__ = ["ApiError", "DroneAirspaceService", "Repository", "create_server", "iso", "parse_time", "utcnow", "main"]


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()

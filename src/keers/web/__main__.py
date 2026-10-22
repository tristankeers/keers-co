"""Run the workbench: ``python -m keers.web [--host 127.0.0.1] [--port 8000]``.

It binds to this machine only by default. Yahoo's data is for personal use, so do not put it on a public address.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from .app import create_app

    ap = argparse.ArgumentParser(prog="python -m keers.web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    uvicorn.run(create_app(), host=a.host, port=a.port)


if __name__ == "__main__":
    main()

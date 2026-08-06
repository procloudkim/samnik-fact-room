from __future__ import annotations

import uvicorn

from .api import create_app

app = create_app()


def run() -> None:
    # The M0 service has no network authentication layer. Keep the console
    # entrypoint loopback-only; an explicit deployment command owns any wider
    # bind together with its reverse proxy and access controls.
    uvicorn.run("samnik_backend.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()

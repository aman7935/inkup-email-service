from __future__ import annotations

import os

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    # Render (and most hosts) tell the app which port to use via $PORT.
    port = int(os.environ.get("PORT") or settings.api_port)
    uvicorn.run(
        "inkup_email_service.main:app",
        host=settings.api_host,
        port=port,
        # Our request middleware replaces uvicorn's access log, and
        # configure_logging() installs the JSON root handler.
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        # One process only: the background sender must not run twice.
        workers=1,
    )


if __name__ == "__main__":
    main()

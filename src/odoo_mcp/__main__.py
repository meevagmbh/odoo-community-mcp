"""Entry point: `python -m odoo_mcp` or `odoo-community-mcp`."""

import uvicorn

from odoo_mcp.app import create_app
from odoo_mcp.config import Settings
from odoo_mcp.logging_setup import configure_logging


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.mcp_host,
        port=settings.mcp_port,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

"""Helpers to drive the ASGI app in-process with a real MCP client."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette


@asynccontextmanager
async def running(app: Starlette) -> AsyncIterator[Starlette]:
    async with app.router.lifespan_context(app):
        yield app


def http(app: Starlette, headers: dict[str, str] | None = None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://mcp.test",
        headers=headers or {},
    )


@asynccontextmanager
async def mcp_client(app: Starlette, key: str) -> AsyncIterator[Client]:
    client = http(app, {"Authorization": f"Bearer {key}"})
    async with (
        client,
        Client(streamable_http_client("http://mcp.test/mcp", http_client=client)) as c,
    ):
        yield c

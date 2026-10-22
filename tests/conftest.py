"""Közös fixture-ök (pytest-homeassistant-custom-component)."""

import sys

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


if sys.platform == "win32":
    # Windowson az eseményhurok belső socketpairt nyit, amit a keretrendszer
    # socket-tiltása megfog. A tesztek hálózatot nem használnak (aioclient_mock),
    # így itt visszaengedjük; Linuxon (CI) a tiltás érvényben marad.
    import asyncio

    import pytest_socket

    import aiohttp.connector
    import aiohttp.resolver

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Az aiodns csak SelectorEventLoop-pal megy Windowson; a mock-session amúgy
    # sem old fel neveket.
    aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver

    pytest_socket.disable_socket = lambda *args, **kwargs: None
    pytest_socket.socket_allow_hosts = lambda *args, **kwargs: None

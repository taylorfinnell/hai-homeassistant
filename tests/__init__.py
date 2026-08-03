"""Tests for the Hai integration.

This package init runs before conftest.py and every test module, which makes
it the one reliable place for Windows-only test accommodations. None of this
executes on the Linux CI runners, which keep all guards fully active.
"""

import sys

if sys.platform == "win32":
    import unittest.mock as _mock

    # The shared test fixtures simulate a Linux host (platform.system() is
    # patched to "Linux"), so bleak and bluetooth_adapters follow their BlueZ
    # code paths. Those import dbus_fast.aio, which cannot import on Windows
    # (socket.CMSG_LEN does not exist). Scanning itself is mocked by the
    # fixtures, so fake D-Bus modules satisfy the imports.
    import dbus_fast as _dbus_fast

    _fake_dbus_aio = _mock.MagicMock()
    for _name in (
        "dbus_fast.aio",
        "dbus_fast.aio.message_bus",
        "dbus_fast.aio.message_reader",
        "dbus_fast.aio.proxy_object",
    ):
        sys.modules[_name] = _fake_dbus_aio
    _dbus_fast.aio = _fake_dbus_aio

    # Present an empty BlueZ advertisement history, exactly like a Linux
    # host that has never seen an advertisement, instead of letting the
    # mocked D-Bus objects leak into history parsing.
    from bluetooth_adapters.systems.linux import LinuxAdapters

    LinuxAdapters.history = property(lambda self: {})

    import socket as _socket

    import pytest_socket as _pytest_socket

    # Windows asyncio backs every event loop with an AF_INET socketpair and
    # there is no AF_UNIX, so the socket guard's allow_unix_socket escape
    # hatch never applies and event loop creation itself would be blocked.
    # Treat local AF_INET as the Windows equivalent of a Unix socket.
    _original_is_unix_socket = _pytest_socket._is_unix_socket

    def _is_unix_socket(family: int) -> bool:
        return family == _socket.AF_INET or _original_is_unix_socket(family)

    _pytest_socket._is_unix_socket = _is_unix_socket

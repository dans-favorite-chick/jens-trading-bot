"""
Tests for bridge :8765 single-stream enforcement (2026-04-25).

Validates that a second concurrent NT8 connection is rejected when
PHOENIX_BRIDGE_SINGLE_STREAM is enabled (the default).

This is the bridge-side defense against the incident class where NT8
auto-loads multiple hidden charts each spawning a TickStreamer instance
(see KNOWN_ISSUES.md → "NT8 multi-stream issue").
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reload_bridge_with_env(monkeypatch, single_stream: str):
    """Set env var and re-import bridge module so it picks up the new value."""
    monkeypatch.setenv("PHOENIX_BRIDGE_SINGLE_STREAM", single_stream)
    import bridge.bridge_server as mod
    importlib.reload(mod)
    return mod


async def _connect_and_send_connect_msg(port: int):
    """Open a TCP socket to bridge and send a TickStreamer-style connect handshake."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    msg = json.dumps({"type": "connect", "instrument": "MNQM6"}) + "\n"
    writer.write(msg.encode("utf-8"))
    await writer.drain()
    return reader, writer


async def _start_bridge_on_free_port(bridge_module):
    """Spin up a real BridgeServer on a free port for the test."""
    bridge = bridge_module.BridgeServer()
    server = await asyncio.start_server(
        bridge.handle_nt8_tcp, "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    return server, bridge, port


def test_single_stream_rejects_second_connection(monkeypatch):
    """When client #1 is connected, client #2 must be closed immediately."""
    bridge_mod = _reload_bridge_with_env(monkeypatch, "1")

    async def _scenario():
        server, bridge, port = await _start_bridge_on_free_port(bridge_mod)
        try:
            # Client #1 — accepted
            r1, w1 = await _connect_and_send_connect_msg(port)
            await asyncio.sleep(0.15)
            assert bridge.nt8_connected is True, \
                "first client should set nt8_connected"

            # Client #2 — rejected (socket closed by bridge)
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
            try:
                data = await asyncio.wait_for(r2.read(64), timeout=2.0)
                # b'' means clean EOF (server closed)
                assert data == b"", \
                    f"second client should get EOF, got {data!r}"
            except asyncio.TimeoutError:
                raise AssertionError("second client was not rejected within 2.0s")

            # Verify rejection event was logged
            events = [e for e in bridge.connection_events
                      if "REJECTED" in e.get("message", "")]
            assert events, \
                f"expected REJECTED event in connection_events, got: {bridge.connection_events}"

            # First client still active
            assert bridge.nt8_connected is True

            # Cleanup
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(_scenario())


def test_single_stream_disabled_allows_multiple(monkeypatch):
    """When PHOENIX_BRIDGE_SINGLE_STREAM=0, multiple connections still allowed."""
    bridge_mod = _reload_bridge_with_env(monkeypatch, "0")

    async def _scenario():
        server, bridge, port = await _start_bridge_on_free_port(bridge_mod)
        try:
            r1, w1 = await _connect_and_send_connect_msg(port)
            await asyncio.sleep(0.15)
            assert bridge.nt8_connected is True

            # Second connection — should NOT be rejected
            r2, w2 = await _connect_and_send_connect_msg(port)
            await asyncio.sleep(0.15)

            events = [e for e in bridge.connection_events
                      if "REJECTED" in e.get("message", "")]
            assert not events, \
                f"with single-stream disabled, no rejection should occur: {events}"

            for w in (w1, w2):
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(_scenario())


def test_single_stream_recovers_after_first_disconnect(monkeypatch):
    """When client #1 disconnects, client #2 should be allowed in."""
    bridge_mod = _reload_bridge_with_env(monkeypatch, "1")

    async def _scenario():
        server, bridge, port = await _start_bridge_on_free_port(bridge_mod)
        try:
            # Client #1 — accepted
            r1, w1 = await _connect_and_send_connect_msg(port)
            await asyncio.sleep(0.15)
            assert bridge.nt8_connected is True

            # Disconnect #1
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass
            # Wait for bridge to process disconnect (the `finally:` block
            # at the bottom of handle_nt8_tcp resets nt8_connected = False)
            await asyncio.sleep(0.4)
            assert bridge.nt8_connected is False, \
                "nt8_connected should reset on disconnect"

            # Client #2 — should now be accepted
            r2, w2 = await _connect_and_send_connect_msg(port)
            await asyncio.sleep(0.15)
            assert bridge.nt8_connected is True, \
                "second client should be accepted after first disconnects"

            w2.close()
            try:
                await w2.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(_scenario())

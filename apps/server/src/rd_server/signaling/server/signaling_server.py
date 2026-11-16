"""Authenticated signaling listener with bounded, owned connection lifetimes."""

import asyncio
import logging
import time

import websockets
from rd_core.auth import AuthorizationError, AuthProvider
from rd_core.logging import get_logger
from rd_core.settings import SignalingSettings
from websockets import ConnectionClosed

from rd_server.signaling.services import RoomManager

logger = get_logger(__name__)


class SignalingServer:
    def __init__(
        self,
        config: SignalingSettings,
        *,
        tls_context=None,
        auth: AuthProvider | None = None,
        allow_legacy_development=False,
    ):
        if auth is None and not allow_legacy_development:
            raise ValueError("signaling requires authentication")
        if allow_legacy_development and config.host not in {"127.0.0.1", "::1"}:
            raise ValueError("legacy signaling requires loopback")
        self.config = config
        self.tls_context = tls_context
        self.auth = auth
        self.manager = RoomManager(config.max_peers_per_room, config.send_timeout_s)
        self._server = None

    async def _expire(self, peer):
        # Recheck wall-clock expiry so clock adjustments cannot extend authorization.
        while True:
            remaining = peer.principal.expires_at - time.time()
            if remaining <= 0:
                await peer.close(1008, "credentials expired")
                return
            await asyncio.sleep(min(remaining, 1.0))

    async def _handle_connection(self, websocket):
        principal = None
        if self.auth is not None:
            try:
                values = websocket.request.headers.get_all("Authorization")
                if len(values) != 1 or not values[0].startswith("Bearer "):
                    raise AuthorizationError("invalid credentials")
                principal = self.auth.authenticate(values[0][7:])
            except AuthorizationError:
                await websocket.close(1008, "invalid credentials")
                return
        peer = await self.manager.register_peer(websocket, principal)
        expiry = None
        if principal is not None and principal.expires_at is not None:
            expiry = asyncio.create_task(self._expire(peer), name="signaling-expiry")
        try:
            async for message in websocket:
                await self.manager.handle_message(peer, message)
        except ConnectionClosed:
            pass
        except Exception as error:  # noqa: BLE001 - connection boundary; sanitize diagnostics
            # Do not log payloads, headers or exception text containing credentials/SDP.
            logger.error("Signaling peer %s failed (%s)", peer.id, type(error).__name__)
            await peer.close(1011, "session failed")
        finally:
            if expiry is not None:
                expiry.cancel()
                await asyncio.gather(expiry, return_exceptions=True)
            await self.manager.unregister_peer(peer)

    async def start(self):
        # websockets DEBUG logs request headers; do not expose bearer tokens even
        # when application DEBUG logging is selected.
        transport_logger = get_logger("rd_server.signaling.transport")
        transport_logger.setLevel(logging.WARNING)
        self._server = await websockets.serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            ping_interval=self.config.ping_interval_s,
            ping_timeout=self.config.ping_timeout_s,
            max_size=self.config.max_message_bytes,
            max_queue=16,
            close_timeout=self.config.close_timeout_s,
            ssl=self.tls_context,
            logger=transport_logger,
        )
        logger.info(
            "Signaling listener ready on %s:%s (TLS=%s)",
            self.config.host,
            self.config.port,
            self.tls_context is not None,
        )

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Signaling listener stopped")

    async def wait_closed(self):
        await self._server.wait_closed()

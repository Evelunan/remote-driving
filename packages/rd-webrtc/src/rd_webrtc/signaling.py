"""One authenticated, bounded WebSocket transport for both media applications."""

import asyncio
import json
import logging
import ssl
from urllib.parse import urlsplit

from aiortc import RTCIceServer
from rd_core.auth import read_secret
from rd_core.configuration import ConfigError
from rd_core.settings import ClientNetworkSettings, CredentialSettings
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


class SignalingError(RuntimeError):
    pass


class _DirectConnect(connect):
    # websockets 16 follows redirects with additional_headers intact. The
    # configured service is the only authorized destination for a bearer token.
    # Keep this version-sensitive hook local and pin the dependency.
    def process_redirect(self, exc):
        if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
            return SignalingError("signaling redirects are disabled")
        return exc


def ice_servers(settings):
    return [
        RTCIceServer(
            urls=list(item.urls),
            username=read_secret(item.username_env) if item.username_env else None,
            credential=read_secret(item.credential_env)
            if item.credential_env
            else None,
        )
        for item in settings
    ]


def tls_context(network: ClientNetworkSettings):
    parsed = urlsplit(network.signaling_url)
    if parsed.scheme == "wss":
        context = ssl.create_default_context(cafile=network.signaling_ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigError("authenticated signaling requires WSS outside loopback")
    return None


def bearer_token(credentials):
    token = read_secret(credentials.token_env)
    if not 32 <= len(token) <= 4096 or any(
        not 33 <= ord(char) <= 126 for char in token
    ):
        raise ConfigError("signaling token must have 32..4096 visible ASCII characters")
    return token


def check_media_config(network, credentials):
    """Validate local dependencies/secrets without opening any network/device."""
    tls_context(network)
    bearer_token(credentials)
    ice_servers(network.ice_servers)


class SignalingClient:
    def __init__(self, network: ClientNetworkSettings, credentials: CredentialSettings):
        self.network, self.credentials = network, credentials
        self.ws = None
        self.handlers = {}
        self._lifecycle = asyncio.Lock()
        self._closing = None
        # Transport DEBUG can print Authorization headers. Application code emits
        # sanitized diagnostics; suppress library payload/header logs entirely.
        self._transport_logger = logging.Logger(  # noqa: LOG001 - unregistered logger cannot inherit payload logging
            "rd_webrtc.transport", logging.CRITICAL + 1
        )
        self._transport_logger.addHandler(logging.NullHandler())
        self._transport_logger.propagate = False

    async def connect(self):
        async with self._lifecycle:
            if self.ws is not None:
                raise RuntimeError("signaling connection is already open")
            network = self.network
            options = {}
            context = tls_context(network)
            if context is not None:
                options["ssl"] = context
            self.ws = await _DirectConnect(
                network.signaling_url,
                additional_headers={
                    "Authorization": "Bearer " + bearer_token(self.credentials)
                },
                open_timeout=network.register_timeout_s,
                ping_interval=network.signaling_ping_interval_s,
                ping_timeout=network.signaling_ping_timeout_s,
                close_timeout=network.signaling_close_timeout_s,
                max_size=network.signaling_max_message_bytes,
                max_queue=16,
                # Do not send local control-system credentials via ambient proxies.
                proxy=None,
                logger=self._transport_logger,
                **options,
            )
            self._closing = None

    async def join(self, room, *, session=None):
        async with asyncio.timeout(self.network.register_timeout_s):
            await self.send(
                {
                    "type": "join",
                    "room": room,
                    **({"session": session} if session is not None else {}),
                }
            )
            reply = await self.recv()
        if reply.get("type") != "joined" or not isinstance(reply.get("id"), str):
            raise SignalingError("room join was not acknowledged")
        peers = reply.get("peers")
        if not isinstance(peers, list) or any(
            not isinstance(peer, str) for peer in peers
        ):
            raise SignalingError("invalid room membership reply")
        return reply

    async def send(self, data):
        if self.ws is None:
            raise ConnectionError("signaling is disconnected")
        try:
            message = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise SignalingError("invalid outgoing signaling message") from None
        if len(message.encode()) > self.network.signaling_max_message_bytes:
            raise SignalingError("outgoing signaling message exceeds limit")
        async with asyncio.timeout(self.network.signaling_send_timeout_s):
            await self.ws.send(message)

    async def recv(self):
        if self.ws is None:
            raise ConnectionError("signaling is disconnected")
        raw = await self.ws.recv()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            raise SignalingError("invalid signaling JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise SignalingError("invalid signaling message")
        if data["type"] in {"error", "full"}:
            # Server text is untrusted; never put it into logs or GUI messages.
            raise SignalingError("signaling request rejected")
        return data

    def on(self, kind, handler):
        self.handlers[kind] = handler

    async def listen(self):
        while True:
            data = await self.recv()
            handler = self.handlers.get(data["type"])
            if handler is not None:
                await handler(data)

    async def close(self):
        async with self._lifecycle:
            if self.ws is None:
                return
            if self._closing is None:
                self._closing = asyncio.create_task(
                    self.ws.close(), name="signaling-close"
                )
            await asyncio.shield(self._closing)
            self.ws = None

"""Shared signaling, peer ownership and reconnect lifecycle for media endpoints."""

import asyncio

from rd_core.async_compat import ExceptionGroup
from rd_core.configuration import ConfigError
from rd_core.logging import get_logger

from .peer import PeerConnectionManager
from .signaling import SignalingClient, ice_servers

logger = get_logger(__name__)


class MediaSession:
    def __init__(self, room, network, credentials, *, publication_session=None):
        self.room, self.network = room, network
        self.publication_session = publication_session
        self.signaling = SignalingClient(network, credentials)
        self._ice_servers = ice_servers(network.ice_servers)
        self.peers = {}
        self.my_id = None
        self._peer_tasks = {}
        self._retired = set()
        self._cleanup_jobs = set()
        self._cleanup_error = None
        self._runner = None
        self._closed = False
        self._close_task = None
        self._disconnect_task = None
        self._status_callback = None
        self.signaling.on("candidate", self._on_candidate)
        self.signaling.on("peer_left", self._on_peer_left)

    def set_status_callback(self, callback):
        self._status_callback = callback

    def _emit_status(self, status, message):
        if self._status_callback is not None:
            self._status_callback(status, message)

    def _current(self, peer_id, peer):
        return not self._closed and self.peers.get(peer_id) is peer

    async def start(self):
        if self._runner is not None or self._closed:
            raise RuntimeError("media session is single-use")
        self._runner = asyncio.create_task(self._run(), name="media-session")
        try:
            await asyncio.shield(self._runner)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _run(self):
        try:
            while not self._closed:
                try:
                    self._emit_status("connecting", "正在连接信令服务器")
                    await self.signaling.connect()
                    joined = await self.signaling.join(
                        self.room, session=self.publication_session
                    )
                    self.my_id = joined["id"]
                    self._emit_status("joined", "信令认证与房间加入已确认")
                    await self._on_joined(joined)
                    await self.signaling.listen()
                except (asyncio.CancelledError, ConfigError):
                    raise
                except Exception as exc:  # noqa: BLE001 - sanitize at reconnect boundary
                    logger.warning("Media connection ended (%s)", type(exc).__name__)
                    self._emit_status("error", "媒体连接失败，等待重连")
                finally:
                    self._disconnect_task = asyncio.create_task(
                        self._disconnect(), name="media-disconnect"
                    )
                    await asyncio.shield(self._disconnect_task)
                await asyncio.sleep(self.network.reconnect_delay_s)
        finally:
            self._closed = True
            try:
                if self._disconnect_task is not None:
                    await asyncio.shield(self._disconnect_task)
            finally:
                self._final_close()
            if self._cleanup_error is not None:
                raise RuntimeError("media peer cleanup failed") from self._cleanup_error

    async def _disconnect(self):
        if self._cleanup_jobs:
            await asyncio.gather(*tuple(self._cleanup_jobs), return_exceptions=True)
        # Remove identity before awaiting cancellation, so late frame/state events
        # cannot mutate the next connection's state.
        peers = set(self.peers.values()) | self._retired
        peer_ids = tuple(self.peers)
        self.peers.clear()
        for peer_id in peer_ids:
            self._peer_removed(peer_id)
        self._retired.update(peers)
        tasks = {task for owned in self._peer_tasks.values() for task in owned}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._peer_tasks.clear()
        results = await asyncio.gather(
            *(peer.close() for peer in peers),
            self.signaling.close(),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        self.my_id = None
        self._emit_status("disconnected", "媒体连接已断开")
        if errors:
            raise ExceptionGroup("media connection cleanup failed", errors)
        self._retired.clear()

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._close_owned(), name="media-close"
            )
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        failure = None
        if self._runner is not None:
            if not self._runner.done():
                self._runner.cancel()
            results = await asyncio.gather(self._runner, return_exceptions=True)
            if isinstance(results[0], Exception):
                failure = results[0]
        try:
            # Also covers cancellation before the runner's first instruction.
            await self._disconnect()
        finally:
            self._final_close()
        if failure is not None:
            raise failure

    def _final_close(self):
        pass

    def _peer_removed(self, peer_id):
        pass

    async def _peer_failed(self, peer_id):
        pass

    async def _remove_peer(self, peer_id, expected=None):
        peer = self.peers.get(peer_id)
        if peer is None or (expected is not None and peer is not expected):
            return False
        self.peers.pop(peer_id)
        self._peer_removed(peer_id)
        self._retired.add(peer)
        tasks = self._peer_tasks.pop(peer, set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await peer.close()
        self._retired.discard(peer)
        return True

    def _schedule_cleanup(self, peer_id, peer):
        if not self._current(peer_id, peer):
            return
        identity = self.my_id

        async def cleanup():
            removed = await self._remove_peer(peer_id, peer)
            # Cleanup can overlap a new offer, disconnect or explicit close.
            # Only the owner of the failed connection may request recovery.
            if (
                removed
                and not self._closed
                and identity is not None
                and self.my_id == identity
                and peer_id not in self.peers
            ):
                await self._peer_failed(peer_id)

        task = asyncio.create_task(cleanup(), name="media-peer-close")
        self._cleanup_jobs.add(task)
        task.add_done_callback(self._cleanup_jobs.discard)

        def observe(done):
            if not done.cancelled() and done.exception() is not None:
                self._cleanup_error = done.exception()
                if self._runner is not None and not self._closed:
                    self._closed = True
                    self._runner.cancel()

        task.add_done_callback(observe)

    async def _create_peer(self, peer_id):
        await self._remove_peer(peer_id)
        peer = PeerConnectionManager(self._ice_servers)
        self.peers[peer_id] = peer
        self._peer_tasks[peer] = set()

        @peer.pc.on("connectionstatechange")
        def on_state():
            if not self._current(peer_id, peer):
                return
            if peer.pc.connectionState == "connected":
                self._emit_status("connected", "WebRTC 握手完成，等待新画面")
            elif peer.pc.connectionState in {"failed", "closed"}:
                self._emit_status("peer_left", "媒体连接失效，等待重新连接")
                self._schedule_cleanup(peer_id, peer)

        return peer

    def _spawn_peer_task(self, peer_id, peer, coroutine, *, name="media-receive"):
        task = asyncio.create_task(coroutine, name=name)
        self._peer_tasks[peer].add(task)

        def observe(done):
            self._peer_tasks.get(peer, set()).discard(done)
            if (
                not done.cancelled()
                and done.exception() is not None
                and self._current(peer_id, peer)
            ):
                logger.warning("%s failed (%s)", name, type(done.exception()).__name__)
                self._emit_status("error", "媒体连接失败")
                self._schedule_cleanup(peer_id, peer)

        task.add_done_callback(observe)
        return task

    async def _on_candidate(self, data):
        peer = self.peers.get(data["from"])
        if peer is not None:
            await peer.add_candidate(data["candidate"])

    async def _on_peer_left(self, data):
        await self._remove_peer(data["id"])
        self._emit_status("peer_left", "对端已离开，等待重新连接")

    async def _on_joined(self, data):
        raise NotImplementedError

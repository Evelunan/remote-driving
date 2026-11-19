"""A connection with a server-owned identity and optional authenticated principal."""

import json
from dataclasses import dataclass
from typing import Any

from rd_core.async_compat import async_timeout
from rd_core.auth import AuthorizationError, Principal
from rd_core.logging import get_logger
from websockets.exceptions import ConnectionClosed

logger = get_logger(__name__)


@dataclass
class Peer:
    id: str
    websocket: Any
    principal: Principal | None = None
    room_id: str | None = None
    send_timeout_s: float = 3.0
    publication_session: int | None = None

    @property
    def video_source(self):
        if (
            self.principal is not None
            and self.principal.role == "vehicle"
            and self.publication_session is not None
        ):
            return {"principal": self.principal.id, "session": self.publication_session}
        return None

    async def send(self, message: dict[str, Any]) -> bool:
        try:
            if self.principal is not None:
                self.principal.check_active()
            async with async_timeout(self.send_timeout_s):
                await self.websocket.send(json.dumps(message))
            return True
        except AuthorizationError:
            await self.close(1008, "credentials expired")
        except TimeoutError:
            await self.close(1013, "slow consumer")
        except ConnectionClosed:
            pass
        return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except ConnectionClosed:
            pass

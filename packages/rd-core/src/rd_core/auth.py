"""Shared authentication and scope checks for signaling and control services."""

import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .configuration import ConfigError
from .settings import AuthSettings, Role


class AuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role
    sessions: frozenset[int]
    rooms: frozenset[str]
    expires_at: float | None = None

    def check_active(self):
        if self.expires_at is not None and time.time() >= self.expires_at:
            raise AuthorizationError("credentials expired")

    def check_session(self, session: int, *, role: Role | None = None):
        self.check_active()
        if session not in self.sessions or (role is not None and role != self.role):
            raise AuthorizationError("session access denied")

    def check_room(self, room: str):
        self.check_active()
        if room not in self.rooms:
            raise AuthorizationError("room access denied")


@dataclass(frozen=True)
class _Credential:
    principal: Principal
    digest: bytes = field(repr=False)


def read_secret(name: str, environ: Mapping[str, str] | None = None) -> str:
    value = (os.environ if environ is None else environ).get(name)
    if not value:
        raise ConfigError(f"missing credential environment variable: {name}")
    return value


class AuthProvider:
    def __init__(
        self, settings: AuthSettings, *, environ: Mapping[str, str] | None = None
    ):
        credentials = []
        seen = set()
        for record in settings.principals:
            secret = read_secret(record.token_env, environ)
            if len(secret) < 32 or len(secret) > 4096:
                raise ConfigError(
                    f"credential length must be 32..4096 characters: {record.token_env}"
                )
            digest = hashlib.sha256(secret.encode("utf-8")).digest()
            if digest in seen:
                raise ConfigError("each principal must have a distinct credential")
            seen.add(digest)
            principal = Principal(
                record.id,
                record.role,
                frozenset(record.sessions),
                frozenset(record.rooms),
                record.expiry_timestamp(),
            )
            credentials.append(_Credential(principal, digest))
        self._credentials = tuple(credentials)

    def authenticate(self, token: str, *, now: float | None = None) -> Principal:
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise AuthorizationError("invalid credentials")
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        current = time.time() if now is None else now
        for record in self._credentials:
            if hmac.compare_digest(digest, record.digest):
                if (
                    record.principal.expires_at is None
                    or current < record.principal.expires_at
                ):
                    return record.principal
                break
        raise AuthorizationError("invalid credentials")

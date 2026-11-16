"""One bounded, versioned JSON message per reliable QUIC stream."""

import json
import math
import struct

from rd_core.wire import VERSION


class RpcError(Exception):
    def __init__(self, code: str):
        if not isinstance(code, str) or len(code) > 64 or not code.isidentifier():
            code = "invalid_error_code"
        self.code = code
        super().__init__(code)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON value")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON value")
    return result


def encode_message(message: dict, max_bytes: int):
    try:
        body = json.dumps(
            {**message, "v": VERSION}, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise RpcError("invalid_message") from None
    if not 0 < len(body) <= max_bytes:
        raise RpcError("message_too_large")
    return struct.pack("!I", len(body)) + body


class MessageBuffer:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.data = bytearray()

    def feed(self, data: bytes, *, end_stream: bool):
        if len(self.data) + len(data) > self.max_bytes + 4:
            raise RpcError("message_too_large")
        self.data.extend(data)
        expected = (
            struct.unpack_from("!I", self.data)[0] if len(self.data) >= 4 else None
        )
        if expected is not None and (
            not 0 < expected <= self.max_bytes or len(self.data) > expected + 4
        ):
            raise RpcError("invalid_length")
        if not end_stream:
            return None
        if expected is None or len(self.data) != expected + 4:
            raise RpcError("truncated_message")
        try:
            message = json.loads(
                self.data[4:],
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
        except (ValueError, RecursionError):
            raise RpcError("invalid_json") from None
        if (
            not isinstance(message, dict)
            or type(message.get("v")) is not int
            or message["v"] != VERSION
        ):
            raise RpcError("unsupported_version")
        kind = message.get("type")
        if kind in ("request", "event"):
            method, params = message.get("method"), message.get("params")
            if (
                set(message) != {"v", "type", "method", "params"}
                or not isinstance(method, str)
                or not method.isidentifier()
                or len(method) > 64
                or not isinstance(params, dict)
            ):
                raise RpcError("invalid_request")
        elif kind == "response":
            if (
                set(message) == {"v", "type", "result"}
                and isinstance(message["result"], dict)
                or set(message) == {"v", "type", "error"}
                and isinstance(message["error"], str)
                and len(message["error"]) <= 64
                and message["error"].isidentifier()
            ):
                pass
            else:
                raise RpcError("invalid_response")
        else:
            raise RpcError("invalid_message_type")
        return message

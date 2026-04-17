"""msgpack wire codec for the qtcnc ZMQ protocol.

Every message that crosses process/network boundaries goes through this
module. It has two layers:

1. **Dataclass <-> dict**: `to_wire()` recursively converts dataclasses,
   enums, and tuples into plain types; `from_wire()` reconstructs the
   typed value given a target class. IntEnums round-trip as ints,
   StrEnums as strings, tuples as lists.

2. **Envelope <-> bytes**: `encode_envelope()` / `decode_envelope()`
   wrap a (MessageType, payload) pair in an envelope with a protocol
   version, a monotonic id, an optional client uuid, and msgpack-pack
   the whole thing. The inverse pulls the envelope fields out and
   returns them as an `Envelope` dataclass.

Protocol minor versions are additive-only: a daemon at (1, N+k) may send
fields that a client at (1, N) has never heard of. `_dataclass_from_dict`
silently drops unknown keys so the older side still reconstructs the
slice of the dataclass it does know about. A field that only lands in a
newer schema falls back to the dataclass default on the older side.

No Qt imports. No pyzmq imports. Pure-Python + msgpack so both the
daemon and the client can share this file and so tests run without
any IPC setup.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, IntEnum
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

import msgpack

from qtcnc import PROTOCOL_VERSION
from qtcnc.signals import MessageType


T = TypeVar("T")


# get_type_hints() is slow (parses annotations). Cache per class.
_hints_cache: dict[type, dict[str, Any]] = {}


def _resolved_hints(cls: type) -> dict[str, Any]:
    hints = _hints_cache.get(cls)
    if hints is None:
        # include_extras not needed; we do not use Annotated[] in our schema.
        hints = get_type_hints(cls)
        _hints_cache[cls] = hints
    return hints


class CodecError(Exception):
    """Base class for codec-level failures."""


class UnknownMessageType(CodecError):
    """The wire envelope carried a tag that is not a known MessageType."""

    def __init__(self, tag: Any):
        super().__init__(f"unknown message type tag: {tag!r}")
        self.tag = tag


class MalformedEnvelope(CodecError):
    """The wire bytes did not decode to a valid envelope shape."""


class FieldMismatch(CodecError):
    """A dataclass reconstruction was missing a required field or had an extra one."""


# ---------------------------------------------------------------------------
# dataclass <-> dict
# ---------------------------------------------------------------------------


def to_wire(value: Any) -> Any:
    """Recursively convert a Python value into msgpack-friendly primitives.

    - dataclass -> dict of field_name -> to_wire(field_value)
    - IntEnum -> int
    - StrEnum / str-valued Enum -> str
    - tuple -> list (msgpack has no tuple type)
    - list -> list of to_wire(item)
    - dict -> dict of key -> to_wire(value)
    - None, bool, int, float, str, bytes -> returned unchanged
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        # Note: IntEnum is an int subclass, so catch it first below.
        if isinstance(value, IntEnum):
            return int(value)
        if isinstance(value, Enum):
            return value.value
        return value
    if isinstance(value, Enum):
        # Non-int enum (e.g. StrEnum). StrEnum is also a str, handled above,
        # but explicit for readability on non-str / non-int enums.
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_wire(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [to_wire(v) for v in value]
    if isinstance(value, frozenset):
        return [to_wire(v) for v in sorted(value)]
    if isinstance(value, list):
        return [to_wire(v) for v in value]
    if isinstance(value, dict):
        return {k: to_wire(v) for k, v in value.items()}
    raise CodecError(f"cannot encode value of type {type(value).__name__}: {value!r}")


def from_wire(value: Any, target: Any) -> Any:
    """Reconstruct a typed Python value from its msgpack-decoded form.

    `target` is the annotation we want the result to satisfy:

    - a dataclass type -> call `cls(**{field: from_wire(value[field], field_type)})`
    - an Enum type -> call `cls(value)`
    - `tuple[T, ...]` -> tuple(from_wire(v, T) for v in value)
    - `list[T]` -> [from_wire(v, T) for v in value]
    - `dict[K, V]` -> {k: from_wire(v, V) for k, v in value.items()}
    - `T | None` (or `Optional[T]`) -> None if value is None, else from_wire(value, T)
    - plain builtin -> `cls(value)` (for coercion, e.g. int("3") is wrong — we don't coerce, just cast on match)
    - `Any` / missing annotation -> return value unchanged
    """
    if target is Any or target is None:
        return value

    origin = get_origin(target)
    args = get_args(target)

    # Optional[T]  ==  Union[T, None]
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if value is None:
            return None
        if len(non_none) == 1:
            return from_wire(value, non_none[0])
        # Unions with multiple non-None members: try each in order.
        for candidate in non_none:
            try:
                return from_wire(value, candidate)
            except (CodecError, TypeError, ValueError):
                continue
        raise CodecError(f"no union arm matched value {value!r} for {target!r}")

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(from_wire(v, args[0]) for v in value)
        if args:
            if len(value) != len(args):
                raise CodecError(f"tuple length mismatch: got {len(value)}, want {len(args)}")
            return tuple(from_wire(v, a) for v, a in zip(value, args))
        return tuple(value)

    if origin is frozenset:
        inner = args[0] if args else Any
        return frozenset(from_wire(v, inner) for v in value)

    if origin is list:
        inner = args[0] if args else Any
        return [from_wire(v, inner) for v in value]

    if origin is dict:
        value_t = args[1] if len(args) == 2 else Any
        return {k: from_wire(v, value_t) for k, v in value.items()}

    # Bare type (no origin)
    if isinstance(target, type):
        if is_dataclass(target):
            if not isinstance(value, dict):
                raise CodecError(
                    f"expected dict for dataclass {target.__name__}, got {type(value).__name__}"
                )
            return _dataclass_from_dict(target, value)
        if issubclass(target, Enum):
            try:
                return target(value)
            except ValueError as e:
                raise CodecError(f"invalid enum value {value!r} for {target.__name__}") from e
        # Primitive type match check. Don't coerce: mismatch is a bug upstream.
        if isinstance(value, target):
            return value
        # bool vs int: Python's bool is an int subclass, so `isinstance(True, int)`
        # is True; accept that silently. The reverse (int where bool expected) is
        # allowed too — msgpack round-tripping booleans is reliable.
        if target is float and isinstance(value, int):
            return float(value)
        raise CodecError(
            f"type mismatch: value {value!r} is not an instance of {target.__name__}"
        )

    # Unrecognized annotation form: pass the value through.
    return value


def _dataclass_from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass instance from a dict, validating fields.

    Unknown keys are silently dropped so a (1, N) client can decode a
    payload from a (1, N+k) daemon that added fields under the same
    major version.
    """
    cls_fields = {f.name: f for f in fields(cls)}
    hints = _resolved_hints(cls)
    kwargs: dict[str, Any] = {}
    for name, f in cls_fields.items():
        if name in data:
            kwargs[name] = from_wire(data[name], hints.get(name, Any))
        elif (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        ):
            # Let dataclass use its default.
            continue
        else:
            raise FieldMismatch(f"{cls.__name__} missing required field {name!r}")
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """Wire envelope wrapping every REQ/REP and PUB message payload."""

    v: tuple[int, int]          # protocol version (major, minor)
    id: int                     # monotonic request id (0 for PUB)
    type: MessageType           # message tag
    payload: dict[str, Any]     # already in wire form
    client: str = ""            # client uuid string, "" for PUB


def encode_envelope(
    msg_type: MessageType,
    payload: dict[str, Any] | None = None,
    *,
    id: int = 0,
    client: str = "",
    version: tuple[int, int] = PROTOCOL_VERSION,
) -> bytes:
    """Pack (type, payload) into an msgpack envelope.

    `payload` should already have been fed through `to_wire()` by the
    caller when it contains dataclasses. This function does not re-walk
    the payload; it only wraps it.
    """
    env = {
        "v": list(version),
        "id": int(id),
        "type": str(msg_type),
        "client": client,
        "payload": payload or {},
    }
    return msgpack.packb(env, use_bin_type=True)


def decode_envelope(data: bytes) -> Envelope:
    """Unpack an msgpack envelope into an `Envelope` dataclass.

    Validates shape and the `type` tag. Does NOT reconstruct the payload
    into typed dataclasses — callers do that with `from_wire()` since
    the target type depends on `env.type`.
    """
    try:
        raw = msgpack.unpackb(data, raw=False)
    except Exception as e:
        raise MalformedEnvelope(f"msgpack unpack failed: {e}") from e
    if not isinstance(raw, dict):
        raise MalformedEnvelope(f"envelope is not a map: {type(raw).__name__}")
    for key in ("v", "id", "type", "payload"):
        if key not in raw:
            raise MalformedEnvelope(f"envelope missing required key {key!r}")
    v = raw["v"]
    if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(n, int) for n in v)):
        raise MalformedEnvelope(f"envelope 'v' must be (int, int), got {v!r}")
    tag = raw["type"]
    if not isinstance(tag, str):
        raise MalformedEnvelope(f"envelope 'type' must be str, got {type(tag).__name__}")
    try:
        msg_type = MessageType(tag)
    except ValueError as e:
        raise UnknownMessageType(tag) from e
    payload = raw["payload"]
    if not isinstance(payload, dict):
        raise MalformedEnvelope(f"envelope 'payload' must be a map, got {type(payload).__name__}")
    req_id = raw["id"]
    if not isinstance(req_id, int):
        raise MalformedEnvelope(f"envelope 'id' must be int, got {type(req_id).__name__}")
    client = raw.get("client", "")
    if not isinstance(client, str):
        raise MalformedEnvelope(f"envelope 'client' must be str, got {type(client).__name__}")
    return Envelope(
        v=(int(v[0]), int(v[1])),
        id=req_id,
        type=msg_type,
        payload=payload,
        client=client,
    )


def version_compatible(
    client: tuple[int, int], daemon: tuple[int, int]
) -> bool:
    """Return True if client and daemon can talk.

    Major version must match exactly. Minor version may differ (the peer
    with the higher minor is expected to accept the lower one).
    """
    return client[0] == daemon[0]

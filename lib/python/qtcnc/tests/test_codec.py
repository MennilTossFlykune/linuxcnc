"""Tests for qtcnc.transport.codec — dataclass encode/decode and envelopes."""

from __future__ import annotations

import msgpack
import pytest

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    InterpState,
    MachineState,
    MotionType,
    Overrides,
    Position,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
    Tool,
)
from qtcnc.signals import CommandVerb, Lifecycle, MessageType
from qtcnc.transport.codec import (
    CodecError,
    Envelope,
    FieldMismatch,
    MalformedEnvelope,
    UnknownMessageType,
    decode_envelope,
    encode_envelope,
    from_wire,
    to_wire,
    version_compatible,
)


# ---------------------------------------------------------------------------
# to_wire / from_wire — primitives
# ---------------------------------------------------------------------------


class TestToWirePrimitives:
    def test_none(self):
        assert to_wire(None) is None

    def test_bool(self):
        assert to_wire(True) is True
        assert to_wire(False) is False

    def test_int(self):
        assert to_wire(42) == 42

    def test_float(self):
        assert to_wire(3.14) == 3.14

    def test_str(self):
        assert to_wire("hello") == "hello"

    def test_bytes(self):
        assert to_wire(b"\x01\x02") == b"\x01\x02"

    def test_list(self):
        assert to_wire([1, 2, 3]) == [1, 2, 3]

    def test_dict(self):
        assert to_wire({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_tuple_becomes_list(self):
        assert to_wire((1, 2, 3)) == [1, 2, 3]

    def test_nested(self):
        assert to_wire({"xs": (1, 2), "y": [{"z": 3}]}) == {"xs": [1, 2], "y": [{"z": 3}]}


class TestToWireEnums:
    def test_int_enum(self):
        assert to_wire(TaskMode.AUTO) == 2
        assert to_wire(SpindleDir.REVERSE) == -1

    def test_str_enum(self):
        assert to_wire(CommandVerb.ESTOP) == "estop"
        assert to_wire(MessageType.HELLO) == "hello"
        assert to_wire(Lifecycle.PROGRAM_LOADED) == "program_loaded"


class TestToWireDataclasses:
    def test_simple_dataclass(self):
        p = Position(x=1.0, y=2.0, z=3.0)
        out = to_wire(p)
        assert out == {
            "x": 1.0, "y": 2.0, "z": 3.0,
            "a": None, "b": None, "c": None,
            "u": None, "v": None, "w": None,
        }

    def test_dataclass_with_optional_axes(self):
        p = Position(x=1.0, y=2.0, z=3.0, a=10.0)
        out = to_wire(p)
        assert out["a"] == 10.0
        assert out["b"] is None

    def test_nested_dataclass(self):
        t = Tool(id=5, pocket=3, offset=Position(x=0.1, y=0.2, z=0.3), diameter=6.0)
        out = to_wire(t)
        assert out["id"] == 5
        assert out["pocket"] == 3
        assert out["offset"]["x"] == 0.1
        assert out["offset"]["y"] == 0.2
        assert out["diameter"] == 6.0

    def test_dataclass_with_enum(self):
        m = MachineState(
            estop=False,
            powered=True,
            task_mode=TaskMode.AUTO,
            interp_state=InterpState.READING,
            motion_type=MotionType.FEED,
            homed=(True, True, False),
        )
        out = to_wire(m)
        assert out["task_mode"] == 2
        assert out["interp_state"] == 2
        assert out["motion_type"] == 2
        assert out["homed"] == [True, True, False]

    def test_error_message(self):
        e = ErrorMessage(severity=ErrorSeverity.OPERATOR_ERROR, text="oops", timestamp=1.5)
        out = to_wire(e)
        assert out == {"severity": 2, "text": "oops", "timestamp": 1.5}

    def test_hal_pin_spec(self):
        s = HalPinSpec(
            name="qtcnc.dro_x.value-out",
            type=HalType.FLOAT,
            dir=HalDir.OUT,
            initial=0.0,
            owner_widget="dro_x",
        )
        out = to_wire(s)
        assert out["name"] == "qtcnc.dro_x.value-out"
        assert out["type"] == 2
        assert out["dir"] == 2
        assert out["initial"] == 0.0
        assert out["owner_widget"] == "dro_x"


class TestToWireErrors:
    def test_unknown_type_raises(self):
        class Weird:
            pass
        with pytest.raises(CodecError):
            to_wire(Weird())


# ---------------------------------------------------------------------------
# from_wire — reconstructing dataclasses
# ---------------------------------------------------------------------------


class TestFromWirePrimitives:
    def test_any_passes_through(self):
        from typing import Any
        assert from_wire(42, Any) == 42
        assert from_wire([1, 2], Any) == [1, 2]

    def test_int(self):
        assert from_wire(42, int) == 42

    def test_str(self):
        assert from_wire("abc", str) == "abc"

    def test_float_accepts_int(self):
        assert from_wire(5, float) == 5.0
        assert isinstance(from_wire(5, float), float)

    def test_int_enum(self):
        assert from_wire(2, TaskMode) == TaskMode.AUTO

    def test_str_enum(self):
        assert from_wire("estop", CommandVerb) == CommandVerb.ESTOP

    def test_int_enum_invalid(self):
        with pytest.raises(CodecError):
            from_wire(99, TaskMode)

    def test_type_mismatch(self):
        with pytest.raises(CodecError):
            from_wire("not an int", int)


class TestFromWireOptional:
    def test_none(self):
        assert from_wire(None, "float | None") is None
        assert from_wire(None, Position | None) is None

    def test_some(self):
        # We import the alias as a forward ref here via runtime annotation.
        from typing import Optional
        assert from_wire(1.5, Optional[float]) == 1.5


class TestFromWireCollections:
    def test_tuple_ellipsis(self):
        from typing import Tuple
        assert from_wire([True, False, True], Tuple[bool, ...]) == (True, False, True)

    def test_tuple_ellipsis_enum(self):
        from typing import Tuple
        out = from_wire([1, -1, 0], Tuple[SpindleDir, ...])
        assert out == (SpindleDir.FORWARD, SpindleDir.REVERSE, SpindleDir.STOP)

    def test_list(self):
        from typing import List
        assert from_wire([1, 2, 3], List[int]) == [1, 2, 3]

    def test_dict(self):
        from typing import Dict
        assert from_wire({"a": 1, "b": 2}, Dict[str, int]) == {"a": 1, "b": 2}


class TestFromWireDataclass:
    def test_position_round_trip(self):
        p = Position(x=1.0, y=2.0, z=3.0, a=10.0)
        out = from_wire(to_wire(p), Position)
        assert out == p

    def test_tool_nested_round_trip(self):
        t = Tool(
            id=7,
            pocket=3,
            offset=Position(x=0.1, y=0.2, z=0.3),
            diameter=6.0,
            comment="endmill",
        )
        out = from_wire(to_wire(t), Tool)
        assert out == t

    def test_machine_state_round_trip(self):
        m = MachineState(
            estop=False,
            powered=True,
            task_mode=TaskMode.MDI,
            interp_state=InterpState.WAITING,
            motion_type=MotionType.ARC,
            homed=(True, True, False, True),
            axis_count=4,
        )
        out = from_wire(to_wire(m), MachineState)
        assert out == m

    def test_error_message_round_trip(self):
        e = ErrorMessage(severity=ErrorSeverity.NML_ERROR, text="bad thing", timestamp=123.4)
        out = from_wire(to_wire(e), ErrorMessage)
        assert out == e

    def test_spindle_state_round_trip(self):
        s = SpindleState(index=1, speed=1500.0, direction=SpindleDir.FORWARD, enabled=True)
        out = from_wire(to_wire(s), SpindleState)
        assert out == s

    def test_hal_pin_spec_round_trip(self):
        spec = HalPinSpec(
            name="qtcnc.dro_x.value-out",
            type=HalType.FLOAT,
            dir=HalDir.OUT,
            initial=0.0,
            owner_widget="dro_x",
        )
        out = from_wire(to_wire(spec), HalPinSpec)
        assert out == spec

    def test_state_store_full_round_trip(self):
        s = StateStore(
            connected=True,
            task_state=TaskState.ON,
            machine=MachineState(
                estop=False, powered=True, task_mode=TaskMode.AUTO,
                interp_state=InterpState.READING, motion_type=MotionType.FEED,
                homed=(True, True, True), axis_count=3,
            ),
            position=Position(x=1.0, y=2.0, z=3.0),
            machine_position=Position(x=10.0, y=20.0, z=30.0),
            dtg=Position(x=-1.0, y=-2.0, z=-3.0),
            program=ProgramState(
                path="/tmp/p.ngc", total_lines=50, current_line=5, is_running=True,
            ),
            tool=Tool(id=2, pocket=2, offset=Position(), diameter=6.0, comment="em"),
            tool_in_spindle=2,
            spindles=(SpindleState(index=0, speed=1200, direction=SpindleDir.FORWARD, enabled=True),),
            overrides=Overrides(feed=0.8, rapid=0.5, spindles=(1.2,)),
            feed_rate=80.0,
            rapid_rate=500.0,
            active_gcodes=(20, 90, 17),
            active_mcodes=(3, 8),
        )
        wire = to_wire(s)
        back = from_wire(wire, StateStore)
        assert back == s

    def test_state_store_default_round_trip(self):
        s = StateStore()
        back = from_wire(to_wire(s), StateStore)
        assert back == s


class TestFromWireDataclassErrors:
    def test_unknown_field(self):
        data = {"x": 1.0, "y": 2.0, "z": 3.0, "extra": 99}
        with pytest.raises(FieldMismatch):
            from_wire(data, Position)

    def test_missing_required_field(self):
        # ErrorMessage has all required fields (no defaults)
        with pytest.raises(FieldMismatch):
            from_wire({"severity": 0, "text": "x"}, ErrorMessage)

    def test_non_dict_for_dataclass(self):
        with pytest.raises(CodecError):
            from_wire([1, 2, 3], Position)


# ---------------------------------------------------------------------------
# envelope encode/decode
# ---------------------------------------------------------------------------


class TestEncodeEnvelope:
    def test_produces_bytes(self):
        data = encode_envelope(MessageType.PING, {})
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_roundtrip_minimal(self):
        data = encode_envelope(MessageType.PING, {})
        env = decode_envelope(data)
        assert env.type == MessageType.PING
        assert env.payload == {}
        assert env.v == PROTOCOL_VERSION

    def test_roundtrip_with_payload(self):
        payload = to_wire(Position(x=1.0, y=2.0, z=3.0))
        data = encode_envelope(MessageType.SNAPSHOT, payload, id=42, client="abc")
        env = decode_envelope(data)
        assert env.type == MessageType.SNAPSHOT
        assert env.id == 42
        assert env.client == "abc"
        assert env.payload["x"] == 1.0

    def test_roundtrip_every_message_type(self):
        # Every tag in MessageType must round-trip cleanly.
        for t in MessageType:
            data = encode_envelope(t, {"k": 1})
            env = decode_envelope(data)
            assert env.type == t
            assert env.payload == {"k": 1}

    def test_roundtrip_nested_dataclass_payload(self):
        spec = HalPinSpec(
            name="qtcnc.dro.value-out", type=HalType.FLOAT, dir=HalDir.OUT
        )
        payload = {"specs": to_wire([spec])}
        data = encode_envelope(MessageType.DECLARE_PINS, payload)
        env = decode_envelope(data)
        from typing import List
        back = from_wire(env.payload["specs"], List[HalPinSpec])
        assert back == [spec]


class TestDecodeEnvelopeErrors:
    def test_not_a_dict(self):
        bad = msgpack.packb([1, 2, 3], use_bin_type=True)
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_missing_type(self):
        bad = msgpack.packb({"v": [1, 0], "id": 0, "payload": {}}, use_bin_type=True)
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_missing_v(self):
        bad = msgpack.packb({"type": "ping", "id": 0, "payload": {}}, use_bin_type=True)
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_missing_id(self):
        bad = msgpack.packb({"v": [1, 0], "type": "ping", "payload": {}}, use_bin_type=True)
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_missing_payload(self):
        bad = msgpack.packb({"v": [1, 0], "id": 0, "type": "ping"}, use_bin_type=True)
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_bad_version_shape(self):
        bad = msgpack.packb(
            {"v": "1.0", "id": 0, "type": "ping", "payload": {}}, use_bin_type=True,
        )
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_bad_type_tag(self):
        bad = msgpack.packb(
            {"v": [1, 0], "id": 0, "type": "not_a_real_tag", "payload": {}},
            use_bin_type=True,
        )
        with pytest.raises(UnknownMessageType):
            decode_envelope(bad)

    def test_bad_type_not_str(self):
        bad = msgpack.packb(
            {"v": [1, 0], "id": 0, "type": 42, "payload": {}},
            use_bin_type=True,
        )
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)

    def test_garbage_bytes(self):
        with pytest.raises(MalformedEnvelope):
            decode_envelope(b"\xff\x00not msgpack at all")

    def test_payload_wrong_type(self):
        bad = msgpack.packb(
            {"v": [1, 0], "id": 0, "type": "ping", "payload": "nope"},
            use_bin_type=True,
        )
        with pytest.raises(MalformedEnvelope):
            decode_envelope(bad)


class TestVersionCompatible:
    def test_exact_match(self):
        assert version_compatible((1, 0), (1, 0)) is True

    def test_minor_diff_ok(self):
        assert version_compatible((1, 0), (1, 3)) is True
        assert version_compatible((1, 5), (1, 0)) is True

    def test_major_mismatch(self):
        assert version_compatible((1, 0), (2, 0)) is False
        assert version_compatible((2, 5), (1, 9)) is False

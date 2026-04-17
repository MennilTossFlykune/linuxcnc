"""Tests for qtcnc.signals — StrEnum values and uniqueness."""

from __future__ import annotations

from qtcnc.signals import CommandVerb, Lifecycle, MessageType, Topic


class TestMessageType:
    def test_is_str(self):
        assert isinstance(MessageType.HELLO, str)
        assert MessageType.HELLO == "hello"

    def test_values_unique(self):
        values = [m.value for m in MessageType]
        assert len(values) == len(set(values))

    def test_core_types_present(self):
        for name in ("HELLO", "WELCOME", "ACK", "NACK", "DECLARE_PINS", "STATE_DIFF"):
            assert hasattr(MessageType, name)


class TestTopic:
    def test_values_unique(self):
        values = [t.value for t in Topic]
        assert len(values) == len(set(values))

    def test_dot_separated(self):
        # Topic names use dotted prefixes so SUB filters work cleanly.
        assert "." in Topic.STATE_DIFF
        assert "." in Topic.HAL_PIN


class TestCommandVerb:
    def test_values_unique(self):
        values = [v.value for v in CommandVerb]
        assert len(values) == len(set(values))

    def test_core_verbs_present(self):
        for name in ("STATE_ESTOP", "STATE_ON", "SET_MODE", "HOME", "JOG_CONTINUOUS", "MDI"):
            assert hasattr(CommandVerb, name)


class TestLifecycle:
    def test_values_unique(self):
        values = [l.value for l in Lifecycle]
        assert len(values) == len(set(values))

    def test_program_events_present(self):
        for name in (
            "PROGRAM_LOADING",
            "PROGRAM_LOADED",
            "PROGRAM_LOAD_FAILED",
            "PROGRAM_CLOSED",
            "PROGRAM_MISSING",
        ):
            assert hasattr(Lifecycle, name)

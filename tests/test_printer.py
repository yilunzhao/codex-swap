"""Terminal formatting helpers."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from codex_swap import printer


@pytest.fixture(autouse=True)
def reset_color():
    yield
    printer.set_color(None)


class TestColorPolicy:
    def test_forced_on_and_off(self):
        printer.set_color(True)
        assert "\033[" in printer.bold("x")
        printer.set_color(False)
        assert printer.bold("x") == "x"

    def test_no_color_env_is_respected(self, monkeypatch):
        printer.set_color(None)
        monkeypatch.setenv("NO_COLOR", "1")
        assert printer.color_enabled() is False

    def test_dumb_terminal_is_respected(self, monkeypatch):
        printer.set_color(None)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        assert printer.color_enabled() is False

    def test_non_tty_disables_color(self, monkeypatch):
        printer.set_color(None)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        assert printer.color_enabled() is False

    def test_explicit_override_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.set_color(True)
        assert printer.color_enabled() is True

    @pytest.mark.parametrize(
        "fn", [printer.bold, printer.dim, printer.red, printer.green, printer.yellow, printer.cyan]
    )
    def test_every_style_is_a_no_op_when_disabled(self, fn):
        printer.set_color(False)
        assert fn("text") == "text"


class TestFormatSpan:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (45, "45s"),
            (89, "89s"),
            (90, "1m"),
            (3600, "60m"),
            (5400, "1h"),
            (86400, "24h"),
            (172800, "2d"),
            (864000, "10d"),
        ],
    )
    def test_thresholds(self, seconds, expected):
        assert printer.format_span(seconds) == expected

    def test_negative_values_use_magnitude(self):
        assert printer.format_span(-45) == "45s"


class TestFormatAge:
    def test_past(self):
        now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        when = now - dt.timedelta(hours=3)
        assert printer.format_age(when, now=now) == "3h ago"

    def test_future(self):
        now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        when = now + dt.timedelta(minutes=20)
        assert printer.format_age(when, now=now) == "in 20m"

    def test_none(self):
        assert printer.format_age(None) == "unknown"

    def test_naive_datetimes_are_treated_as_utc(self):
        now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        naive = dt.datetime(2026, 8, 12, 11, 0)
        assert printer.format_age(naive, now=now) == "60m ago"


class TestParseIso:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-08-08T17:33:16.621745Z",
            "2026-08-08T17:33:16Z",
            "2026-08-08T17:33:16+00:00",
            "2026-08-08T17:33:16",
        ],
    )
    def test_accepted_shapes(self, value):
        parsed = printer.parse_iso(value)
        assert parsed is not None and parsed.tzinfo is not None

    @pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45"])
    def test_rejected_shapes(self, value):
        assert printer.parse_iso(value) is None

    def test_round_trip_with_the_models_timestamp(self):
        from codex_swap.models import timestamp

        assert printer.parse_iso(timestamp()) is not None


class TestAbbreviate:
    """Paths are rendered in the platform's own convention, so the expectations
    are built with os.sep rather than hard-coded to POSIX."""

    def test_replaces_home(self, tmp_path):
        expected = os.sep.join(["~", "x", "y"])
        assert printer.abbreviate(tmp_path / "x" / "y", home=tmp_path) == expected

    def test_leaves_other_paths_alone(self, tmp_path):
        other = Path("/etc/hosts")
        assert printer.abbreviate(other, home=tmp_path) == str(other)

    def test_accepts_a_string(self, tmp_path):
        assert printer.abbreviate(str(tmp_path / "z"), home=tmp_path) == os.sep.join(["~", "z"])

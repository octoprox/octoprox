# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MuteBook: which open alert each stored mute hides at one moment, and the mutes a write stores; the alerts job ends
the until-clear ones (tests/db/test_project_repository.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from api.attention.alert import Alert
from api.attention.mutes import MuteBook
from api.core.audit import AuditLog
from api.models.attention import MuteDuration, Severity

NOW = datetime(2026, 11, 16, 14, 2)
KEY = "vendor_rate_limited:c1"
ACTOR = AuditLog().actor("u1", "ivan")


def _alert(severity: Severity = Severity.WARNING, key: str = KEY) -> Alert:
    """An open stored alert of ``key`` at ``severity``."""
    return Alert(
        id=f"a-{key}",
        project_id="p1",
        key=key,
        kind="vendor_rate_limited",
        severity=severity,
        worst_severity=severity,
        lead="",
        subject="Webshare UK",
        text=": the vendor says the account sends too many requests (12% refused in the last 15 minutes).",
        detail=None,
        when=None,
        link="connectors?open=c1",
        action="Open connector",
        sort_name="Webshare UK",
        hidden_by="",
        opened_at=NOW - timedelta(hours=2),
        last_seen_at=NOW,
        resolved_at=None,
        reopened_at=None,
        reopened=0,
    )


def _stored(duration: MuteDuration, *, at: datetime = NOW, severity: Severity = Severity.WARNING) -> dict[str, Any]:
    """A mute as a write stores it, ``duration`` from ``at``."""
    return MuteBook({}, at).written(KEY, _alert(severity), duration, ACTOR)


class TestMuteBook:
    def test_a_timed_mute_hides_its_line_until_it_ends_and_then_nothing(self) -> None:
        stored = _stored(MuteDuration.WEEK)
        assert stored[KEY]["until"] == (NOW + timedelta(days=7)).isoformat()

        mute = MuteBook(stored, NOW + timedelta(days=6)).mute_of(_alert())
        assert mute is not None and mute.active
        assert (mute.severity, mute.duration, mute.until, mute.muted_by, mute.muted_at) == (
            Severity.WARNING, MuteDuration.WEEK, NOW + timedelta(days=7), "ivan", NOW
        )
        assert MuteBook(stored, NOW + timedelta(days=7)).mute_of(_alert()) is None

    def test_each_duration_s_end(self) -> None:
        assert _stored(MuteDuration.DAY)[KEY]["until"] == (NOW + timedelta(days=1)).isoformat()
        assert _stored(MuteDuration.MONTH)[KEY]["until"] == (NOW + timedelta(days=30)).isoformat()
        assert _stored(MuteDuration.UNTIL_CLEAR)[KEY]["until"] is None

    def test_an_until_clear_mute_hides_its_line_with_no_end_of_its_own(self) -> None:
        stored = {
            **MuteBook({}, NOW).written("other", _alert(key="other"), MuteDuration.WEEK, ACTOR),
            **_stored(MuteDuration.UNTIL_CLEAR),
        }
        book = MuteBook(stored, NOW + timedelta(days=400))

        mute = book.mute_of(_alert())
        assert mute is not None and mute.active and mute.until is None
        assert book.mute_of(_alert(key="other")) is None

    def test_a_warning_muted_shows_again_while_it_is_a_problem_and_hides_again_when_it_falls_back(self) -> None:
        book = MuteBook(_stored(MuteDuration.WEEK), NOW + timedelta(hours=1))

        worse = book.mute_of(_alert(Severity.PROBLEM))
        assert worse is not None and not worse.active
        back = book.mute_of(_alert(Severity.WARNING))
        assert back is not None and back.active

    def test_a_problem_muted_stays_hidden_as_a_warning(self) -> None:
        book = MuteBook(_stored(MuteDuration.DAY, severity=Severity.PROBLEM), NOW)

        mute = book.mute_of(_alert(Severity.WARNING))
        assert mute is not None and mute.active

    def test_what_a_problem_count_skips(self) -> None:
        book = MuteBook(_stored(MuteDuration.DAY, severity=Severity.PROBLEM), NOW)

        assert book.hides(KEY, Severity.PROBLEM) and not book.hides("other", Severity.PROBLEM)
        warned = MuteBook(_stored(MuteDuration.DAY), NOW)
        assert not warned.hides(KEY, Severity.PROBLEM) and warned.hides(KEY, Severity.WARNING)

    def test_a_write_replaces_the_key_and_prunes_the_timed_mutes_that_ended(self) -> None:
        ended = MuteBook({}, NOW - timedelta(days=2)).written("ended", _alert(key="ended"), MuteDuration.DAY, ACTOR)
        clearing = MuteBook({}, NOW - timedelta(days=90)).written(
            "clearing", _alert(key="clearing"), MuteDuration.UNTIL_CLEAR, ACTOR
        )
        stored = {**ended, **clearing, **_stored(MuteDuration.DAY)}
        book = MuteBook(stored, NOW)

        written = book.written(KEY, _alert(Severity.PROBLEM), MuteDuration.MONTH, ACTOR)

        assert set(written) == {"clearing", KEY}
        assert written[KEY] == {
            "severity": "problem",
            "duration": "month",
            "until": (NOW + timedelta(days=30)).isoformat(),
            "by": "ivan",
            "by_id": "u1",
            "at": NOW.isoformat(),
            "text": _alert().sentence,
        }
        assert set(book.without(KEY)) == {"clearing"}
        assert set(book.without("never-muted")) == {"clearing", KEY}

    def test_what_is_in_force_and_its_text(self) -> None:
        book = MuteBook(_stored(MuteDuration.DAY), NOW + timedelta(hours=1))

        assert book.holds(KEY) and not book.holds("other")
        assert book.text_of(KEY).startswith("Webshare UK: the vendor says")
        assert not MuteBook(_stored(MuteDuration.DAY), NOW + timedelta(days=1)).holds(KEY)

    def test_a_mute_this_release_cannot_read_hides_nothing_and_is_kept(self) -> None:
        book = MuteBook({KEY: {"severity": "loud"}}, NOW)

        assert book.mute_of(_alert()) is None
        assert book.written("other", _alert(key="other"), MuteDuration.DAY, ACTOR)[KEY] == {"severity": "loud"}

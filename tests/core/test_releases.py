# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Release notes (api.core.releases): the notes file, what a user has not seen, and the update check's finding."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from api.core.releases import RELEASES_FILE, LatestRelease, ReleaseNote, ReleaseNotes, version_key
from api.db.redis import LATEST_RELEASE_KEY, RedisClient

NOTES = """
# A comment, as the file starts with.
- version: 6.1.4
  from: 6.1.0
  date: 2026-10-27
  title: IP sharing and exit headers
  changes:
    - text: "Exit headers name the exit."
      docs: connecting#exit-headers
- version: 6.2.1
  date: 2026-11-03
  title: Fixes
  changes:
    - text: "A fix."
- version: 6.2.0
  date: 2026-11-01
  title: What's New
  changes:
    - text: "Help has What's New."
"""


def note(version: str) -> ReleaseNote:
    return ReleaseNote(version=version, date=date(2026, 11, 1), title=version, changes=[])


class TestVersionKey:
    def test_a_release_orders_by_its_numbers_not_as_text(self) -> None:
        assert version_key("6.10.0") == (6, 10, 0)
        assert version_key("6.10.0") > version_key("6.9.12")  # type: ignore[operator]

    @pytest.mark.parametrize("version", ["unknown", "6.1", "6.1.4.dev1", "", None])
    def test_anything_else_has_none(self, version: str | None) -> None:
        assert version_key(version) is None


class TestReleaseNotes:
    def test_notes_come_newest_first_whatever_the_file_order(self) -> None:
        notes = ReleaseNotes.parse(NOTES)
        assert [n.version for n in notes.notes] == ["6.2.1", "6.2.0", "6.1.4"]
        assert notes.notes[2].from_version == "6.1.0"
        assert notes.notes[2].changes[0].docs == "connecting#exit-headers"

    def test_a_version_that_is_not_major_minor_patch_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ReleaseNotes.parse("- {version: '6.2', date: 2026-11-01, title: T, changes: []}")

    def test_through_keeps_the_running_release_and_older_ones(self) -> None:
        notes = ReleaseNotes.parse(NOTES)
        assert [n.version for n in notes.through("6.2.0")] == ["6.2.0", "6.1.4"]
        # A source tree's version is no release: every note.
        assert [n.version for n in notes.through("unknown")] == ["6.2.1", "6.2.0", "6.1.4"]

    def test_after_keeps_only_newer_releases(self) -> None:
        notes = ReleaseNotes.parse(NOTES)
        assert [n.version for n in notes.after("6.1.4")] == ["6.2.1", "6.2.0"]
        assert notes.after("6.2.1") == []
        assert notes.after("unknown") == []

    def test_unseen_counts_the_notes_since_the_last_one_opened_up_to_the_running_release(self) -> None:
        notes = ReleaseNotes.parse(NOTES)
        assert notes.unseen("6.2.1", "6.1.4") == 2
        assert notes.unseen("6.2.0", "6.1.4") == 1
        assert notes.unseen("6.2.1", "6.2.1") == 0

    def test_someone_who_never_opened_what_s_new_has_only_the_newest_unseen(self) -> None:
        assert ReleaseNotes.parse(NOTES).unseen("6.2.1", None) == 1
        assert ReleaseNotes([]).unseen("6.2.1", None) == 0

    def test_the_notes_file_the_image_ships_parses(self) -> None:
        """What's New, the changelog page and the update check all read it."""
        assert RELEASES_FILE.exists()
        assert ReleaseNotes.parse(RELEASES_FILE.read_text()).notes


class TestLatestRelease:
    def test_the_newest_release_listed_is_its_version(self) -> None:
        payload = [note("6.2.0").model_dump(mode="json", by_alias=True), note("6.3.0").model_dump(mode="json")]
        found = LatestRelease.of(datetime(2026, 11, 2, 9), payload)
        assert found.version == "6.3.0"
        assert LatestRelease(checked_at=datetime(2026, 11, 2), notes=[]).version is None

    def test_a_payload_that_is_not_a_list_of_notes_is_refused(self) -> None:
        with pytest.raises(ValueError):
            LatestRelease.of(datetime(2026, 11, 2), {"version": "6.3.0"})

    def test_an_entry_this_release_cannot_read_is_skipped_not_the_whole_list(self) -> None:
        payload = [
            {"version": "7.0.0-rc.1", "date": "2026-12-01", "title": "T", "changes": []},
            note("6.3.0").model_dump(mode="json"),
        ]
        assert LatestRelease.of(datetime(2026, 11, 2), payload).version == "6.3.0"

    async def test_a_finding_this_release_cannot_read_loads_as_none(self, redis_client: RedisClient) -> None:
        await redis_client.client.set(LATEST_RELEASE_KEY, '{"checked_at": "2026-11-02T09:00:00", "notes": [{"version": "7"}]}')
        assert await LatestRelease.load(redis_client) is None

    async def test_a_finding_saved_is_loaded_back_with_its_ranges(self, redis_client: RedisClient) -> None:
        assert await LatestRelease.load(redis_client) is None
        summary = ReleaseNote.model_validate(
            {"version": "6.1.4", "from": "6.1.0", "date": "2026-10-27", "title": "T", "changes": []}
        )
        found = LatestRelease(checked_at=datetime(2026, 11, 2, 9), notes=[note("6.2.0"), summary])
        await found.save(redis_client)
        loaded = await LatestRelease.load(redis_client)
        assert loaded == found
        assert loaded is not None and loaded.notes[1].from_version == "6.1.0"

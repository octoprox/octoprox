# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""AlertRepository: the alerts job's one statement a run, which opens, refreshes, resolves and reopens alerts and is
safe to repeat, and the reads of the card, the project list and the alert history."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.attention.alert import REOPEN_WINDOW, Alert, FoundAlert
from api.attention.checks import AttentionKind, Finding
from api.attention.install import InstallKind
from api.core import utc_now
from api.db.alert_repository import AlertFilters, AlertRepository, ReconcileCounts
from api.db.models import AlertModel
from api.db.repository import ProjectRepository
from api.models.alerts import AlertSettings
from api.models.attention import AttentionWhen, Severity
from api.models.project import Project

KEY = "vendor_rate_limited:c1"


def finding(
    key: str = KEY,
    severity: Severity = Severity.WARNING,
    *,
    subject: str = "Webshare UK",
    text: str = ": the vendor says the account sends too many requests (12% refused in the last 15 minutes).",
    when: AttentionWhen | None = None,
) -> Finding:
    kind = key.partition(":")[0]
    return Finding(
        key=key,
        kind=InstallKind(kind) if kind in InstallKind._value2member_map_ else AttentionKind(kind),
        severity=severity,
        lead="",
        subject=subject,
        text=text,
        link="connectors?open=c1",
        action="Open connector",
        detail="The vendor answered 429 to 61 of 508 requests.",
        when=when,
        sort_name=subject,
    )


class Store:
    """Runs of the reconcile, each in its own transaction as the job runs it, and the rows they leave."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], projects: list[Project]) -> None:
        self.sessions = sessions
        self.one, self.two = (project.id for project in projects)

    async def run(
        self,
        now: datetime,
        found: list[FoundAlert],
        checked: list[str],
        held: list[tuple[str, str]] | None = None,
    ) -> ReconcileCounts:
        async with self.sessions() as session:
            counts = await AlertRepository(session).reconcile(found, checked, held or [], now, reopen=REOPEN_WINDOW)
            await session.commit()
        return counts

    async def rows(self, scope: str | None = None) -> list[AlertModel]:
        """Every alert of ``scope`` (a project id, '' for the install, None for all), oldest first."""
        async with self.sessions() as session:
            query = select(AlertModel).order_by(AlertModel.opened_at, AlertModel.id)
            if scope == "":
                query = query.where(AlertModel.project_id.is_(None))
            elif scope is not None:
                query = query.where(AlertModel.project_id == scope)
            return list((await session.execute(query)).scalars())

    async def only(self, scope: str | None = None) -> AlertModel:
        rows = await self.rows(scope)
        assert len(rows) == 1, rows
        return rows[0]


@pytest.fixture
async def store(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]) -> Store:
    projects = [Project(name=f"Alerting {i}", username=f"alerting{i}", password="pass") for i in range(2)]
    for project in projects:
        await ProjectRepository(db_session).create(project)
    await db_session.commit()
    return Store(db_session_factory, projects)


class TestReconcile:
    async def test_a_new_condition_opens(self, store: Store) -> None:
        now = utc_now()
        at = now - timedelta(minutes=1)

        counts = await store.run(now, [FoundAlert(store.one, finding(when=AttentionWhen(kind="last_at", at=at)))], [store.one])

        assert counts == ReconcileCounts(opened=1)
        row = await store.only()
        assert (row.project_id, row.key, row.kind, row.severity, row.worst_severity) == (
            store.one, KEY, "vendor_rate_limited", "warning", "warning"
        )
        assert (row.opened_at, row.last_seen_at, row.resolved_at, row.reopened_at, row.reopened_count) == (
            now, now, None, None, 0
        )
        assert (row.subject, row.body, row.when_kind, row.when_at, row.hidden_by) == (
            "Webshare UK", finding().text, "last_at", at, ""
        )

    async def test_found_again_it_is_refreshed_and_keeps_its_worst(self, store: Store) -> None:
        now = utc_now()
        await store.run(now, [FoundAlert(store.one, finding(severity=Severity.PROBLEM))], [store.one])
        later = now + timedelta(minutes=1)

        counts = await store.run(
            later,
            [FoundAlert(store.one, finding(subject="Webshare US", text=" new words."), hidden_by="connectors_off")],
            [store.one],
        )

        assert counts == ReconcileCounts(kept=1)
        row = await store.only()
        assert (row.severity, row.worst_severity, row.subject, row.body, row.hidden_by) == (
            "warning", "problem", "Webshare US", " new words.", "connectors_off"
        )
        assert (row.opened_at, row.last_seen_at) == (now, later)

    async def test_gone_it_resolves_in_the_scopes_checked_only(self, store: Store) -> None:
        now = utc_now()
        await store.run(
            now,
            [FoundAlert(store.one, finding()), FoundAlert(store.two, finding()), FoundAlert("", finding("background_behind"))],
            [store.one, store.two, ""],
        )
        later = now + timedelta(minutes=1)

        counts = await store.run(later, [], [store.one])

        assert counts == ReconcileCounts(resolved=1)
        assert (await store.only(store.one)).resolved_at == later
        assert (await store.only(store.two)).resolved_at is None
        assert (await store.only("")).resolved_at is None

    async def test_a_held_key_neither_resolves_nor_refreshes(self, store: Store) -> None:
        now = utc_now()
        await store.run(now, [FoundAlert("", finding("changes_not_applied")), FoundAlert("", finding("ip_databases"))], [""])
        later = now + timedelta(minutes=1)

        counts = await store.run(later, [], [""], held=[("", "changes_not_applied")])

        assert counts == ReconcileCounts(resolved=1)
        rows = {row.key: row for row in await store.rows("")}
        assert (rows["changes_not_applied"].resolved_at, rows["changes_not_applied"].last_seen_at) == (None, now)
        assert rows["ip_databases"].resolved_at == later

    async def test_back_within_the_window_it_reopens_and_after_it_a_new_alert_opens(self, store: Store) -> None:
        now = utc_now()
        await store.run(now, [FoundAlert(store.one, finding())], [store.one])
        resolved = now + timedelta(minutes=1)
        await store.run(resolved, [], [store.one])
        back = resolved + REOPEN_WINDOW

        counts = await store.run(back, [FoundAlert(store.one, finding(severity=Severity.PROBLEM))], [store.one])

        assert counts == ReconcileCounts(reopened=1)
        row = await store.only()
        assert (row.opened_at, row.reopened_at, row.reopened_count, row.resolved_at, row.last_seen_at) == (
            now, back, 1, None, back
        )
        assert (row.severity, row.worst_severity) == ("problem", "problem")

        gone = back + timedelta(minutes=1)
        await store.run(gone, [], [store.one])
        late = gone + REOPEN_WINDOW + timedelta(seconds=1)
        assert await store.run(late, [FoundAlert(store.one, finding())], [store.one]) == ReconcileCounts(opened=1)
        rows = await store.rows()
        assert [(row.opened_at, row.resolved_at) for row in rows] == [(now, gone), (late, None)]

    async def test_an_older_run_changes_nothing(self, store: Store) -> None:
        now = utc_now()
        await store.run(now, [FoundAlert(store.one, finding()), FoundAlert(store.one, finding("refusals:x"))], [store.one])
        resolved = now + timedelta(minutes=2)
        await store.run(resolved, [FoundAlert(store.one, finding())], [store.one])
        before = {row.key: (row.last_seen_at, row.resolved_at, row.subject) for row in await store.rows()}
        older = now + timedelta(minutes=1)

        # It would refresh the open one, resolve it, and reopen the resolved one: each guard compares with its :now.
        counts = await store.run(older, [FoundAlert(store.one, finding("refusals:x", subject="stale"))], [store.one])

        assert counts == ReconcileCounts()
        assert {row.key: (row.last_seen_at, row.resolved_at, row.subject) for row in await store.rows()} == before

    async def test_the_same_run_twice_stores_the_same_alerts(self, store: Store) -> None:
        now = utc_now()
        await store.run(now - timedelta(minutes=1), [FoundAlert(store.one, finding("refusals:x"))], [store.one])
        run = ([FoundAlert(store.one, finding()), FoundAlert(store.two, finding())], [store.one, store.two])

        first = await store.run(now, *run)
        stored = [(row.id, row.last_seen_at, row.resolved_at) for row in await store.rows()]
        second = await store.run(now, *run)

        assert first == ReconcileCounts(opened=2, resolved=1)
        assert second == ReconcileCounts()
        assert [(row.id, row.last_seen_at, row.resolved_at) for row in await store.rows()] == stored

    async def test_two_scopes_and_the_install_in_one_run(self, store: Store) -> None:
        now = utc_now()

        counts = await store.run(
            now,
            [FoundAlert(store.one, finding()), FoundAlert(store.two, finding()), FoundAlert("", finding("ip_databases"))],
            [store.one, store.two, ""],
        )

        assert counts == ReconcileCounts(opened=3)
        assert [(row.project_id, row.key) for row in await store.rows(store.one)] == [(store.one, KEY)]
        assert [(row.project_id, row.key) for row in await store.rows("")] == [(None, "ip_databases")]

    async def test_no_alert_for_a_project_deleted_before_the_statement(self, store: Store) -> None:
        async with store.sessions() as session:
            await ProjectRepository(session).delete(store.two)
            await session.commit()

        counts = await store.run(utc_now(), [FoundAlert(store.two, finding()), FoundAlert(store.one, finding())], [store.one, store.two])

        assert counts == ReconcileCounts(opened=1)
        assert [row.project_id for row in await store.rows()] == [store.one]


class TestReads:
    async def test_the_card_skips_covered_lines_and_the_list_counts_shown_problems(self, store: Store) -> None:
        now = utc_now()
        await store.run(
            now,
            [
                FoundAlert(store.one, finding("connectors_off", Severity.PROBLEM)),
                FoundAlert(store.one, finding("refusals:no_proxy_available", Severity.PROBLEM), hidden_by="connectors_off"),
                FoundAlert(store.two, finding(KEY, Severity.WARNING)),
                FoundAlert("", finding("no_gateway_serving", Severity.PROBLEM)),
            ],
            [store.one, store.two, ""],
        )

        async with store.sessions() as session:
            alerts = AlertRepository(session)
            assert [alert.key for alert in await alerts.open_of(store.one)] == ["connectors_off"]
            assert sorted(alert.key for alert in await alerts.open_of(store.one, shown=False)) == [
                "connectors_off", "refusals:no_proxy_available"
            ]
            assert await alerts.open_problems() == [(store.one, "connectors_off")]
            covered = await alerts.open_one(store.one, "refusals:no_proxy_available")
            assert covered is not None and covered.hidden_by == "connectors_off"
            assert await alerts.open_one(store.two, "connectors_off") is None
            assert [alert.key for alert in await alerts.open_install()] == ["no_gateway_serving"]


class TestPage:
    @pytest.fixture
    async def history(self, store: Store) -> tuple[Store, datetime]:
        """Five alerts of project one, a minute apart: two still open (one covered), three resolved, one of them a
        problem at its worst; and one of project two."""
        start = utc_now() - timedelta(hours=1)
        runs = [
            [FoundAlert(store.one, finding("vendor_login:a", Severity.PROBLEM, subject="100% off_site"))],
            [FoundAlert(store.one, finding("vendor_login:b", subject="Plain_Proxies"))],
            [FoundAlert(store.one, finding(KEY, subject="Webshare UK"))],
            [FoundAlert(store.one, finding("refusals:x", subject="10 requests"))],
            [FoundAlert(store.one, finding("connector_no_healthy_proxy:c", subject="Dead"), hidden_by="connector_no_healthy_proxy")],
        ]
        for minute, found in enumerate(runs):
            async with store.sessions() as session:
                await AlertRepository(session).reconcile(
                    found, [], [], start + timedelta(minutes=minute), reopen=REOPEN_WINDOW
                )
                await session.commit()
        # The first three resolve, the first after falling back to a warning.
        await store.run(
            start + timedelta(minutes=10), [FoundAlert(store.one, finding("vendor_login:a", subject="100% off_site"))], []
        )
        async with store.sessions() as session:
            await session.execute(
                AlertModel.__table__.update()  # type: ignore[attr-defined]
                .where(AlertModel.key.in_(["vendor_login:a", "vendor_login:b", KEY]))
                .values(resolved_at=start + timedelta(minutes=20))
            )
            await session.commit()
        await store.run(start, [FoundAlert(store.two, finding())], [])
        return store, start

    async def _page(self, store: Store, filters: AlertFilters, **kwargs: object) -> tuple[list[str], list[int]]:
        async with store.sessions() as session:
            rows = await AlertRepository(session).page(
                store.one, filters, until=kwargs.pop("until", utc_now()), limit=kwargs.pop("limit", 50), **kwargs  # type: ignore[arg-type]
            )
        return [alert.key for alert in rows.alerts], [rows.open, rows.resolved, rows.newer, int(rows.more)]

    async def test_newest_first_with_the_counts(self, history: tuple[Store, datetime]) -> None:
        store, _start = history

        keys, counts = await self._page(store, AlertFilters())

        assert keys == ["connector_no_healthy_proxy:c", "refusals:x", KEY, "vendor_login:b", "vendor_login:a"]
        assert counts == [2, 3, 0, 0]

    async def test_status_and_severity_now_while_open_and_worst_once_resolved(self, history: tuple[Store, datetime]) -> None:
        store, _start = history

        assert (await self._page(store, AlertFilters(status="open")))[0] == ["connector_no_healthy_proxy:c", "refusals:x"]
        keys, counts = await self._page(store, AlertFilters(status="resolved"))
        assert keys == [KEY, "vendor_login:b", "vendor_login:a"]
        # The counts are under every filter but the status.
        assert counts[:2] == [2, 3]
        assert (await self._page(store, AlertFilters(severity=Severity.PROBLEM)))[0] == ["vendor_login:a"]
        assert (await self._page(store, AlertFilters(kind="vendor_login")))[0] == ["vendor_login:b", "vendor_login:a"]

    async def test_the_subject_search_takes_percent_and_underscore_as_text(self, history: tuple[Store, datetime]) -> None:
        store, _start = history

        assert (await self._page(store, AlertFilters(q="100%")))[0] == ["vendor_login:a"]
        assert (await self._page(store, AlertFilters(q="n_P")))[0] == ["vendor_login:b"]
        assert (await self._page(store, AlertFilters(q="webshare")))[0] == [KEY]

    async def test_since_keeps_whatever_was_open_since_then(self, history: tuple[Store, datetime]) -> None:
        store, start = history

        keys, counts = await self._page(store, AlertFilters(since=start + timedelta(minutes=21)))

        assert keys == ["connector_no_healthy_proxy:c", "refusals:x"]
        assert counts[:2] == [2, 0]
        assert len((await self._page(store, AlertFilters(since=start + timedelta(minutes=20))))[0]) == 5

    async def test_until_holds_the_page_and_newer_counts_what_opened_since(self, history: tuple[Store, datetime]) -> None:
        store, start = history

        keys, counts = await self._page(store, AlertFilters(), until=start + timedelta(minutes=2))

        assert keys == [KEY, "vendor_login:b", "vendor_login:a"]
        assert counts == [2, 3, 2, 0]

    async def test_the_cursor_reads_on_before_the_last_alert_shown(self, history: tuple[Store, datetime]) -> None:
        store, _start = history

        first, counts = await self._page(store, AlertFilters(), limit=2)
        assert first == ["connector_no_healthy_proxy:c", "refusals:x"] and counts[3] == 1
        async with store.sessions() as session:
            anchor = await AlertRepository(session).open_one(store.one, "refusals:x")
        assert anchor is not None
        rest, counts = await self._page(store, AlertFilters(), limit=2, before=anchor.id)
        assert rest == [KEY, "vendor_login:b"] and counts[3] == 1

    async def test_one_alert_of_the_scope_asked(self, history: tuple[Store, datetime]) -> None:
        store, _start = history
        async with store.sessions() as session:
            alerts = AlertRepository(session)
            open_one = await alerts.open_one(store.one, "refusals:x")
            assert open_one is not None
            found = await alerts.one(store.one, open_one.id)
            assert isinstance(found, Alert) and found.key == "refusals:x"
            assert await alerts.one(store.two, open_one.id) is None
            assert await alerts.one(None, open_one.id) is None


class TestRetention:
    async def test_resolved_alerts_go_by_each_project_s_days_and_the_install_s(
        self, store: Store, db_session: AsyncSession
    ) -> None:
        now = utc_now()
        old = now - timedelta(days=100)
        await store.run(old, [FoundAlert(store.one, finding()), FoundAlert(store.two, finding()), FoundAlert("", finding("ip_databases"))], [])
        await store.run(old + timedelta(minutes=1), [], [store.one, store.two, ""])
        # A project keeping its alerts forever, and an open alert, which never goes.
        async with store.sessions() as session:
            await ProjectRepository(session).write_alert_settings(store.two, AlertSettings(keep_days=0))
            await session.commit()
        await store.run(old + timedelta(minutes=2), [FoundAlert(store.one, finding("refusals:x"))], [])

        async with store.sessions() as session:
            alerts = AlertRepository(session)
            assert await alerts.delete_resolved_of_projects(now) == 1
            assert await alerts.delete_resolved_of_install(now - timedelta(days=90)) == 1
            await session.commit()

        assert [(row.project_id, row.key, row.resolved_at) for row in await store.rows()] == [
            (store.two, KEY, old + timedelta(minutes=1)),
            (store.one, "refusals:x", None),
        ]


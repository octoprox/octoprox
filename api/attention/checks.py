# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project lines of Needs attention: one check per family of lines, each reading a project's stored state and
finding what fails now (a problem) or is degraded or about to be (a warning).

Thresholds are fixed here; :func:`build_report` drops what a worse line makes redundant and puts the rest in a stable
order, so a refresh only changes words in place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from api.core.stats import FailureKind
from api.geo.models import META_LOCATION_VERDICT, ContradictedAction, LocationVerdict
from api.geo.service import GeoService
from api.models.attention import AttentionLine, AttentionMute, AttentionWhen, Severity
from api.models.connector import BYTES_PER_GB, TrafficLimitAction, TrafficPeriod
from api.models.location import LocationTarget
from api.models.proxy import ProxyStatus
from api.providers.sdk.strategies import META_SESSION_ID, is_dynamic_gateway
from api.reputation.split import place_label

if TYPE_CHECKING:
    from api.attention.data import FailureCount, ProjectData
    from api.models.connector import Connector
    from api.reputation.record import ConnectorSiteReputation

# The request lines' window.
WINDOW = timedelta(minutes=15)
# A request line whose newest failure row is older is dropped.
FRESH = timedelta(minutes=5)
# Every vendor and refusal line needs this many.
MIN_REFUSED = 10
# A connector_failing line needs this many requests.
MIN_FAILING_REQUESTS = 50
STUCK_DELETING = timedelta(minutes=30)
REPUTATION_RECENT = timedelta(hours=1)
# As many sites as Exit locations' reputation table (MAX_TABLE_SITES).
REPUTATION_SITES = 8
# connector_no_healthy_proxy folds into one line past this.
FOLD_CONNECTORS_ABOVE = 3

# What a connector's proxies' failures say, the request metrics having counted them; the vendor lines read
# upstream_rejected, and a client hanging up or a cut is no fault of the connector.
_FAILING_REASONS: dict[str, str] = {
    FailureKind.UPSTREAM_TIMEOUT: "requests timed out",
    FailureKind.UPSTREAM_UNREACHABLE: "its proxies could not be reached",
    FailureKind.UPSTREAM_ERROR: "connections through it dropped",
    FailureKind.PREFLIGHT_MISMATCH: "exits were not in the place asked",
}
# Rows still waiting for their first health check.
_AWAITING = frozenset({ProxyStatus.UNKNOWN, ProxyStatus.INITIALIZING})
_PERIODS = {TrafficPeriod.DAY: "today", TrafficPeriod.WEEK: "this week", TrafficPeriod.MONTH: "this month"}


class AttentionKind(StrEnum):
    """A kind of project line, in the order lines of one severity show."""

    CONNECTORS_OFF = "connectors_off"
    POOL_NO_ROUTABLE = "pool_no_routable"
    COUNTRIES_UNSERVED = "countries_unserved"
    VENDOR_LOGIN = "vendor_login"
    VENDOR_RATE_LIMITED = "vendor_rate_limited"
    VENDOR_FORBIDDEN = "vendor_forbidden"
    VENDOR_CANNOT_SERVE = "vendor_cannot_serve"
    VENDOR_REFUSED_OTHER = "vendor_refused_other"
    CONNECTOR_FAILING = "connector_failing"
    REFUSALS = "refusals"
    CONNECTOR_SYNC_FAILING = "connector_sync_failing"
    CONNECTOR_CLOUD_ERROR = "connector_cloud_error"
    CONNECTOR_NO_HEALTHY_PROXY = "connector_no_healthy_proxy"
    CONNECTOR_TRAFFIC_BLOCKED = "connector_traffic_blocked"
    CONNECTOR_TRAFFIC_NEAR = "connector_traffic_near"
    CONNECTOR_TRAFFIC_OVER_ALERT = "connector_traffic_over_alert"
    POOL_UNHEALTHY_SHARE = "pool_unhealthy_share"
    EXITS_CONTRADICTED = "exits_contradicted"
    EXITS_OUTSIDE_COUNTRIES = "exits_outside_countries"
    REPUTATION_SITE_BLOCKED = "reputation_site_blocked"
    CONNECTOR_STUCK_DELETING = "connector_stuck_deleting"


_KIND_ORDER = {kind: position for position, kind in enumerate(AttentionKind)}


@dataclass(frozen=True)
class Finding:
    """One line a check found, before its mute is looked up; what it makes redundant goes with it."""

    key: str
    kind: AttentionKind
    severity: Severity
    lead: str
    subject: str
    text: str
    link: str
    action: str
    detail: str | None = None
    when: AttentionWhen | None = None
    # What orders it among lines of its kind: a connector's name, a site, a fixed index.
    sort_name: str = ""
    # Line keys or kinds it makes redundant.
    suppresses: frozenset[str] = frozenset()
    # Keys of the lines a folded line stands for.
    covers: frozenset[str] = frozenset()

    @property
    def sentence(self) -> str:
        """The whole line, as History records it."""
        return self.lead + self.subject + self.text

    def line(self, mute: AttentionMute | None) -> AttentionLine:
        """The line as the api answers it, with its mute."""
        return AttentionLine(
            key=self.key,
            kind=self.kind.value,
            severity=self.severity,
            lead=self.lead,
            subject=self.subject,
            text=self.text,
            detail=self.detail,
            when=self.when,
            link=self.link,
            action=self.action,
            mute=mute,
        )


class AttentionCheck(ABC):
    """One family of lines: the findings it reads off one project's data."""

    kinds: ClassVar[tuple[AttentionKind, ...]]
    # Whether one of its lines can be a problem: the project list's counts run only these.
    can_be_problem: ClassVar[bool] = False

    @abstractmethod
    def findings(self, data: ProjectData) -> list[Finding]:
        """The lines this family finds in the project now."""


class ConnectorCheck(AttentionCheck):
    """A family with one line per connector, opening that connector."""

    def findings(self, data: ProjectData) -> list[Finding]:
        return [finding for connector in data.connectors for finding in self._of(connector, data)]

    @abstractmethod
    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        """The connector's lines of this family."""


class RequestWindowCheck(AttentionCheck):
    """A family read off the last 15 minutes of failure counts: fresh rows only, shares of a request count."""

    @staticmethod
    def _fresh(rows: Collection[FailureCount], now: datetime) -> bool:
        """Whether the newest of ``rows`` is recent enough for a line: a request line goes five minutes after it."""
        return bool(rows) and RequestWindowCheck._last_at(rows) >= now - FRESH

    @staticmethod
    def _share(n: int, total: int) -> float:
        """``n`` of ``total``, the total never below ``n``: a window not flushed yet never divides by zero."""
        whole = max(total, n)
        return n / whole if whole else 0.0

    @staticmethod
    def _last_at(rows: Collection[FailureCount]) -> datetime:
        return max(row.last_at for row in rows)

    @staticmethod
    def _when(rows: Collection[FailureCount]) -> AttentionWhen:
        return AttentionWhen(kind="last_at", at=RequestWindowCheck._last_at(rows))


# --- Lines ----------------------------------------------------------------------------------------


class ConnectorsOffCheck(AttentionCheck):
    """No connector, or every connector turned off: nothing serves the project."""

    kinds = (AttentionKind.CONNECTORS_OFF,)
    can_be_problem = True

    def findings(self, data: ProjectData) -> list[Finding]:
        live = [connector for connector in data.connectors if not connector.pending_deletion]
        if not live:
            text = "Add a connector to start serving requests."
        elif not any(connector.enabled for connector in live):
            text = "Every connector is turned off, so all requests fail."
        else:
            return []
        return [
            Finding(
                key=AttentionKind.CONNECTORS_OFF.value,
                kind=AttentionKind.CONNECTORS_OFF,
                severity=Severity.PROBLEM,
                lead="",
                subject="",
                text=text,
                link="connectors",
                action="Connectors",
                suppresses=frozenset({
                    AttentionKind.POOL_NO_ROUTABLE,
                    AttentionKind.COUNTRIES_UNSERVED,
                    AttentionKind.CONNECTOR_NO_HEALTHY_PROXY,
                    AttentionKind.POOL_UNHEALTHY_SHARE,
                    _refusal_key(FailureKind.NO_PROXY_AVAILABLE),
                    _refusal_key(FailureKind.ALL_PROXIES_QUARANTINED),
                    _refusal_key(FailureKind.TRAFFIC_LIMIT_EXCEEDED),
                }),
            )
        ]


class PoolCheck(AttentionCheck):
    """The project's pool as the hero counts it: nothing can take requests, or many proxies keep failing their checks."""

    kinds = (AttentionKind.POOL_NO_ROUTABLE, AttentionKind.POOL_UNHEALTHY_SHARE)
    can_be_problem = True

    def findings(self, data: ProjectData) -> list[Finding]:
        return [finding for finding in (self._no_routable(data), self._unhealthy_share(data)) if finding is not None]

    def _no_routable(self, data: ProjectData) -> Finding | None:
        """Nothing can take requests: every taker over its traffic limit, or no counted row healthy and free."""
        takers = [connector for connector in data.connectors if _takes_requests(connector)]
        suppresses = {
            AttentionKind.CONNECTOR_NO_HEALTHY_PROXY,
            AttentionKind.POOL_UNHEALTHY_SHARE,
            AttentionKind.COUNTRIES_UNSERVED,
            _refusal_key(FailureKind.NO_PROXY_AVAILABLE),
        }
        if takers and all(connector.id in data.blocked for connector in takers):
            return _pool_finding(
                "Every connector",
                " is over its traffic limit, so requests are turned away until a limit resets or is raised.",
                link="connectors",
                action="Connectors",
                suppresses=suppresses | {_refusal_key(FailureKind.TRAFFIC_LIMIT_EXCEEDED)},
            )
        pool = data.pool
        counted = Counter[ProxyStatus]()
        for connector in takers:
            if connector.id not in data.blocked:
                counted.update(data.statuses.get(connector.id, Counter()))
        settled = any(count and not status.retiring and status not in _AWAITING for status, count in counted.items())
        if pool.total == 0 or pool.healthy > 0 or not settled:
            return None
        # The hero's breakdown: a quarantined row counts as quarantined only.
        unhealthy = max(0, pool.unhealthy - pool.quarantined)
        quarantined = pool.quarantined
        if quarantined and not unhealthy:
            return _pool_finding(
                "Every proxy",
                " is quarantined by its rate limit, so clients are told to retry later.",
                link="proxies",
                action="Proxies",
                suppresses=suppresses | {_refusal_key(FailureKind.ALL_PROXIES_QUARANTINED)},
            )
        resting = f", {quarantined} quarantined by rate limits" if quarantined else ""
        return _pool_finding(
            "No proxy can take requests",
            f": {unhealthy} unhealthy{resting}. Requests fail until one recovers.",
            link="proxies",
            action="Proxies",
            suppresses=suppresses,
        )

    @staticmethod
    def _unhealthy_share(data: ProjectData) -> Finding | None:
        """A quarter or more of the counted rows keep failing their health checks, outside the connectors that have no
        healthy row at all (their own line)."""
        rows = 0
        failing: Counter[str] = Counter()
        for connector in data.connectors:
            statuses = data.statuses.get(connector.id, Counter())
            if (
                not _takes_requests(connector)
                or connector.id in data.blocked
                or NoHealthyProxyCheck.qualifies(statuses)
            ):
                continue
            rows += sum(
                count for status, count in statuses.items() if not status.retiring and status not in _AWAITING
            )
            failing[connector.id] = statuses[ProxyStatus.UNHEALTHY]
        unhealthy = sum(failing.values())
        if unhealthy < 3 or unhealthy / rows < 0.25:
            return None
        names = {connector.id: connector.name for connector in data.connectors}
        most = max(failing, key=lambda connector_id: failing[connector_id])
        return Finding(
            key=AttentionKind.POOL_UNHEALTHY_SHARE.value,
            kind=AttentionKind.POOL_UNHEALTHY_SHARE,
            severity=Severity.WARNING,
            lead="",
            subject=f"{unhealthy} of {rows} proxies",
            text=f" keep failing health checks and get no requests, most on {names[most]}.",
            link="proxies",
            action="Proxies",
        )


class CountriesCheck(AttentionCheck):
    """Countries a connector lists or reserves that nothing can serve now, as the Countries page finds them."""

    kinds = (AttentionKind.COUNTRIES_UNSERVED,)
    can_be_problem = True

    def findings(self, data: ProjectData) -> list[Finding]:
        project_id = data.project.id
        named: set[str] = set()
        for view in data.preview.snapshot.connector_views(project_id):
            if view.connector.pending_deletion:
                continue
            if view.takes_requests:
                named.update(view.declared_countries)
            # A reservation holds the country even while its connector is off: the others never serve it.
            named.update(view.reserved)
        failing = [
            code for code in named if not data.preview.route_preview(project_id, LocationTarget(country=code))
        ]
        if not failing:
            return []
        names = sorted((place_label(code), code) for code in failing)
        return [
            Finding(
                key=AttentionKind.COUNTRIES_UNSERVED.value,
                kind=AttentionKind.COUNTRIES_UNSERVED,
                severity=Severity.PROBLEM,
                lead="Requests for ",
                subject=_countries([name for name, _code in names]),
                text=f" fail: no connector can serve {'it' if len(names) == 1 else 'them'} right now.",
                link=f"countries?country={names[0][1]}" if len(names) == 1 else "countries",
                action="Countries",
            )
        ]


class VendorRefusalCheck(RequestWindowCheck):
    """A connector's vendor refusing its requests in the window, by what the vendor's status says."""

    kinds = (
        AttentionKind.VENDOR_LOGIN,
        AttentionKind.VENDOR_RATE_LIMITED,
        AttentionKind.VENDOR_FORBIDDEN,
        AttentionKind.VENDOR_CANNOT_SERVE,
        AttentionKind.VENDOR_REFUSED_OTHER,
    )
    can_be_problem = True
    # The vendor statuses each kind reads; the last kind takes every other status, and none.
    _STATUSES: ClassVar[dict[AttentionKind, frozenset[int]]] = {
        AttentionKind.VENDOR_LOGIN: frozenset({407}),
        AttentionKind.VENDOR_RATE_LIMITED: frozenset({429}),
        AttentionKind.VENDOR_FORBIDDEN: frozenset({403}),
        AttentionKind.VENDOR_CANNOT_SERVE: frozenset({400, 502, 503}),
    }

    def findings(self, data: ProjectData) -> list[Finding]:
        found: list[Finding] = []
        for connector in data.connectors:
            if not _takes_requests(connector):
                continue
            rejected = [
                row
                for row in data.failures
                if row.connector_id == connector.id and row.kind == FailureKind.UPSTREAM_REJECTED
            ]
            by_kind: dict[AttentionKind, list[FailureCount]] = {}
            for row in rejected:
                by_kind.setdefault(self._kind_of(row.upstream_status), []).append(row)
            requests = data.connector_requests.get(connector.id, 0)
            for kind, rows in by_kind.items():
                finding = self._finding(kind, connector, rows, requests, data.now)
                if finding is not None:
                    found.append(finding)
        return found

    def _kind_of(self, status: int | None) -> AttentionKind:
        return next(
            (kind for kind, statuses in self._STATUSES.items() if status in statuses),
            AttentionKind.VENDOR_REFUSED_OTHER,
        )

    def _finding(
        self, kind: AttentionKind, connector: Connector, rows: list[FailureCount], requests: int, now: datetime
    ) -> Finding | None:
        n = sum(row.count for row in rows)
        share = self._share(n, requests)
        total = max(requests, n)
        severity = self._severity(kind, share)
        if n < MIN_REFUSED or severity is None or not self._fresh(rows, now):
            return None
        pct = round(share * 100)
        if kind == AttentionKind.VENDOR_LOGIN:
            text = f": the vendor is rejecting its login ({pct}% of requests in the last 15 minutes)."
            detail = f"The vendor answered 407 to {n} of {total} requests."
        elif kind == AttentionKind.VENDOR_RATE_LIMITED:
            text = f": the vendor says the account sends too many requests ({pct}% refused in the last 15 minutes)."
            detail = f"The vendor answered 429 to {n} of {total} requests."
        elif kind == AttentionKind.VENDOR_FORBIDDEN:
            text = (
                ": the vendor does not let the account reach some sites or ports "
                f"({pct}% refused in the last 15 minutes)."
            )
            detail = f"The vendor answered 403 to {n} of {total} requests."
        elif kind == AttentionKind.VENDOR_CANNOT_SERVE:
            text = (
                f": the vendor could not serve {pct}% of its requests in the last 15 minutes, "
                "for example a place it has no exit in."
            )
            detail = f"The vendor answered 400, 502 or 503 to {n} of {total} requests."
        else:
            text = f": the vendor refused {pct}% of its requests in the last 15 minutes."
            statuses: Counter[int | None] = Counter()
            for row in rows:
                statuses[row.upstream_status] += row.count
            most = statuses.most_common(1)[0][0]
            answer = f"status {most}" if most is not None else "no status"
            detail = f"The vendor refused {n} of {total} requests, most often with {answer}."
        return _connector_finding(
            kind, connector, severity, text, detail=detail, when=self._when(rows)
        )

    @staticmethod
    def _severity(kind: AttentionKind, share: float) -> Severity | None:
        """How bad a share of refusals of ``kind`` is; None under its cut-off."""
        if kind == AttentionKind.VENDOR_LOGIN:
            return Severity.PROBLEM if share >= 0.20 else None
        if kind in (AttentionKind.VENDOR_RATE_LIMITED, AttentionKind.VENDOR_FORBIDDEN):
            if share >= 0.50:
                return Severity.PROBLEM
            return Severity.WARNING if share >= 0.05 else None
        return Severity.WARNING if share >= 0.30 else None


class ConnectorFailingCheck(RequestWindowCheck):
    """A connector whose proxies fail a large share of its requests in the window, by the main reason."""

    kinds = (AttentionKind.CONNECTOR_FAILING,)
    can_be_problem = True

    def findings(self, data: ProjectData) -> list[Finding]:
        found: list[Finding] = []
        for connector in data.connectors:
            if not _takes_requests(connector):
                continue
            rows = [
                row for row in data.failures if row.connector_id == connector.id and row.kind in _FAILING_REASONS
            ]
            failed = sum(row.count for row in rows)
            total = max(data.connector_requests.get(connector.id, 0), failed)
            share = self._share(failed, total)
            if total < MIN_FAILING_REQUESTS or share < 0.30 or not self._fresh(rows, data.now):
                continue
            by_kind: Counter[str] = Counter()
            for row in rows:
                by_kind[row.kind] += row.count
            # The largest kind; a tie goes to the one named first.
            main = max(_FAILING_REASONS, key=lambda kind: by_kind[kind])
            found.append(
                _connector_finding(
                    AttentionKind.CONNECTOR_FAILING,
                    connector,
                    Severity.PROBLEM if share >= 0.80 else Severity.WARNING,
                    f": {round(share * 100)}% of its requests failed in the last 15 minutes, "
                    f"mostly because {_FAILING_REASONS[main]}.",
                    detail=f"{failed} of {total} requests failed.",
                    when=self._when(rows),
                )
            )
        return found


class RefusalCheck(RequestWindowCheck):
    """Requests the project's gateways refused in the window, a line per kind of refusal."""

    kinds = (AttentionKind.REFUSALS,)
    can_be_problem = True
    # Each kind read, in the order its lines show, and the rest of its sentence.
    _TEXTS: ClassVar[dict[str, str]] = {
        FailureKind.NO_PROXY_AVAILABLE: (
            " in the last 15 minutes failed: no proxy matched the country, city or proxy they asked for."
        ),
        FailureKind.ALL_PROXIES_QUARANTINED: (
            " in the last 15 minutes were told to retry later: every proxy they could use was quarantined by its "
            "rate limit."
        ),
        FailureKind.TRAFFIC_LIMIT_EXCEEDED: (
            " in the last 15 minutes were turned away or cut off because their connectors are over their traffic "
            "limits."
        ),
        FailureKind.EXITS_AVOIDED: (
            " in the last 15 minutes failed: every exit they could use is avoided for that site after bad reports."
        ),
        FailureKind.INVALID_LOCATION: (
            " in the last 15 minutes asked for a location that could not be read, such as an unknown country, state "
            "or city. Check the client's username."
        ),
    }

    def findings(self, data: ProjectData) -> list[Finding]:
        # Requests the metrics counted, and those refused before a proxy, which they did not.
        attempts = data.project_requests + sum(row.count for row in data.failures if row.connector_id is None)
        found: list[Finding] = []
        for index, (kind, text) in enumerate(self._TEXTS.items()):
            rows = [row for row in data.failures if row.kind == kind]
            n = sum(row.count for row in rows)
            share = self._share(n, attempts)
            severity = self._severity(kind, share)
            if n < MIN_REFUSED or severity is None or not self._fresh(rows, data.now):
                continue
            found.append(
                Finding(
                    key=_refusal_key(kind),
                    kind=AttentionKind.REFUSALS,
                    severity=severity,
                    lead="",
                    subject=f"{n} requests ({round(share * 100)}%)",
                    text=text,
                    link="failures",
                    action="Failures",
                    when=self._when(rows),
                    # By kind, never by count, so a changing count never moves a row.
                    sort_name=f"{index:02d}",
                )
            )
        return found

    @staticmethod
    def _severity(kind: str, share: float) -> Severity | None:
        """How bad a share of refusals of ``kind`` is: a location that could not be read is a warning at any share."""
        if kind == FailureKind.INVALID_LOCATION:
            return Severity.WARNING
        if share >= 0.25:
            return Severity.PROBLEM
        return Severity.WARNING if share >= 0.05 else None


class ConnectorErrorCheck(ConnectorCheck):
    """A connector whose vendor list or port sync, or whose cloud instances' management, failed the last times."""

    kinds = (AttentionKind.CONNECTOR_SYNC_FAILING, AttentionKind.CONNECTOR_CLOUD_ERROR)

    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        if not _takes_requests(connector) or not connector.last_error or connector.consecutive_errors < 1:
            return []
        n = connector.consecutive_errors
        times = "the last time" if n == 1 else f"the last {n} times"
        if data.registry.is_syncable(connector.credential_type):
            kind = AttentionKind.CONNECTOR_SYNC_FAILING
            text = f" could not get its proxy list from the vendor {times}. It keeps the proxies it had."
        elif data.registry.is_cloud(connector.credential_type):
            kind = AttentionKind.CONNECTOR_CLOUD_ERROR
            text = f" could not start, stop or check its cloud instances {times}. Running instances keep serving."
        else:
            return []
        when = AttentionWhen(kind="last_failed", at=connector.last_error_at) if connector.last_error_at else None
        return [_connector_finding(kind, connector, Severity.WARNING, text, detail=connector.last_error, when=when)]


class NoHealthyProxyCheck(ConnectorCheck):
    """A connector that takes requests with no healthy proxy, every one checked and failing; folded past three."""

    kinds = (AttentionKind.CONNECTOR_NO_HEALTHY_PROXY,)

    @staticmethod
    def qualifies(statuses: Counter[ProxyStatus]) -> bool:
        """Whether a connector's rows say it has no healthy proxy: some unhealthy, none healthy or awaiting a check."""
        return (
            statuses[ProxyStatus.HEALTHY] == 0
            and statuses[ProxyStatus.UNHEALTHY] >= 1
            and not any(statuses[status] for status in _AWAITING)
        )

    def findings(self, data: ProjectData) -> list[Finding]:
        found = super().findings(data)
        if len(found) <= FOLD_CONNECTORS_ABOVE:
            return found
        names = sorted((finding.subject for finding in found), key=str.casefold)
        return [
            Finding(
                key=AttentionKind.CONNECTOR_NO_HEALTHY_PROXY.value,
                kind=AttentionKind.CONNECTOR_NO_HEALTHY_PROXY,
                severity=Severity.WARNING,
                lead="",
                subject=f"{len(names)} connectors",
                text=(
                    f" have no healthy proxy, so they take no requests: {names[0]}, {names[1]} "
                    f"and {len(names) - 2} more."
                ),
                link="connectors",
                action="Connectors",
                covers=frozenset(finding.key for finding in found),
            )
        ]

    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        if (
            not _takes_requests(connector)
            or connector.id in data.blocked
            or not self.qualifies(data.statuses.get(connector.id, Counter()))
        ):
            return []
        return [
            _connector_finding(
                AttentionKind.CONNECTOR_NO_HEALTHY_PROXY,
                connector,
                Severity.WARNING,
                " has no healthy proxy, so it takes no requests.",
            )
        ]


class TrafficCheck(ConnectorCheck):
    """A connector's traffic against its limit this period: blocked, near a blocking limit, or over an alert-only one."""

    kinds = (
        AttentionKind.CONNECTOR_TRAFFIC_BLOCKED,
        AttentionKind.CONNECTOR_TRAFFIC_NEAR,
        AttentionKind.CONNECTOR_TRAFFIC_OVER_ALERT,
    )

    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        usage = data.traffic.get(connector.id)
        if not _takes_requests(connector) or usage is None or usage.limit_bytes is None or usage.pending:
            return []
        limit, period = _volume(usage.limit_bytes), _PERIODS[usage.period]
        pct = round(usage.total_bytes / usage.limit_bytes * 100)
        if usage.blocked:
            kind = AttentionKind.CONNECTOR_TRAFFIC_BLOCKED
            text = (
                f" has used its {limit} limit {period} and takes no new requests until it resets or the limit is "
                "raised."
            )
        elif usage.status == "warning" and usage.action != TrafficLimitAction.ALERT:
            kind = AttentionKind.CONNECTOR_TRAFFIC_NEAR
            text = f" has used {pct}% of its {limit} limit {period}. New requests stop at the limit."
        elif usage.status == "exceeded" and usage.action == TrafficLimitAction.ALERT:
            kind = AttentionKind.CONNECTOR_TRAFFIC_OVER_ALERT
            text = (
                f" is at {pct}% of its {limit} limit {period}. The limit is set to alert only, so it keeps taking "
                "requests."
            )
        else:
            return []
        return [
            _connector_finding(
                kind, connector, Severity.WARNING, text, when=AttentionWhen(kind="resets", at=usage.period_end)
            )
        ]


class ExitLocationCheck(ConnectorCheck):
    """A connector whose fixed exits are, by attribution, outside the countries it lists or not where its vendor says."""

    kinds = (AttentionKind.EXITS_OUTSIDE_COUNTRIES, AttentionKind.EXITS_CONTRADICTED)

    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        view = data.preview.snapshot.connector_view(connector.id)
        if view is None or not view.takes_requests:
            return []
        rows = [proxy for proxy in view.proxies if not proxy.status.retiring and not is_dynamic_gateway(proxy)]
        outside = {
            proxy.id for proxy in rows if GeoService.outside_declared_countries(proxy, view.declared_countries)
        }
        found: list[Finding] = []
        if len(outside) >= 3 and len(outside) / len(rows) >= 0.10:
            found.append(
                _connector_finding(
                    AttentionKind.EXITS_OUTSIDE_COUNTRIES,
                    connector,
                    Severity.WARNING,
                    f": {len(outside)} of {len(rows)} proxies are in countries the connector does not list, so they "
                    "take no requests.",
                )
            )
        rule = data.project.location_contradicted
        if rule == ContradictedAction.EVIDENCE:
            return found
        judged = [
            proxy
            for proxy in rows
            if proxy.metadata.get(META_LOCATION_VERDICT)
            and proxy.id not in outside
            # Refused session slots are replaced within seconds.
            and not (rule == ContradictedAction.REFUSE and proxy.metadata.get(META_SESSION_ID))
        ]
        n = sum(1 for proxy in judged if proxy.metadata.get(META_LOCATION_VERDICT) == LocationVerdict.CONTRADICTED)
        if n >= 3 and n / len(judged) >= 0.25:
            effect = (
                "so they are skipped"
                if rule == ContradictedAction.REFUSE
                else "so some requests for that country are served from elsewhere"
            )
            found.append(
                _connector_finding(
                    AttentionKind.EXITS_CONTRADICTED,
                    connector,
                    Severity.WARNING,
                    f": {n} of {len(judged)} checked proxies are not in the country the vendor says, {effect}.",
                )
            )
        return found


class ReputationCheck(AttentionCheck):
    """A site blocking at least half of the requests on every connector that serves it, by exit reputation."""

    kinds = (AttentionKind.REPUTATION_SITE_BLOCKED,)

    def findings(self, data: ProjectData) -> list[Finding]:
        reputation = data.reputation
        if reputation is None:
            return []
        names = {connector.id: connector.name for connector in data.connectors}
        found: list[Finding] = []
        for site in reputation.sites:
            serving = list(reputation.split.groups_for(site))
            if not serving:
                continue
            shares = reputation.split.shares(site, serving)
            values = [
                self._dynamic_value(reputation.coarse, connector_id, site)
                if reputation.split.dynamic(connector_id)
                else shares[connector_id].reputation.value
                for connector_id in serving
            ]
            if any(value > 0.5 for value in values):
                continue
            text = (
                f" is blocking at least half of the requests through {names.get(serving[0], serving[0])}."
                if len(serving) == 1
                else " is blocking at least half of the requests on every connector."
            )
            found.append(
                Finding(
                    key=f"{AttentionKind.REPUTATION_SITE_BLOCKED.value}:{site}",
                    kind=AttentionKind.REPUTATION_SITE_BLOCKED,
                    severity=Severity.WARNING,
                    lead="",
                    subject=site,
                    text=text,
                    link="locations",
                    action="Exit locations",
                    sort_name=site,
                )
            )
        return found

    @staticmethod
    def _dynamic_value(records: Iterable[ConnectorSiteReputation], connector_id: str, site: str) -> float:
        """A dynamic connector's factor for ``site`` across a country or no place, weighed by the requests behind each;
        1 when no record holds a factor yet."""
        weighed = [
            (record.factor, record.weighted_requests)
            for record in records
            if record.connector_id == connector_id and record.site == site and record.factor is not None
        ]
        if not weighed:
            return 1.0
        weight = sum(requests for _factor, requests in weighed)
        if weight <= 0:
            return sum(factor for factor, _requests in weighed) / len(weighed)
        return sum(factor * requests for factor, requests in weighed) / weight


class StuckDeletingCheck(ConnectorCheck):
    """A connector still being deleted long after it was asked: its cloud instances have not all shut down."""

    kinds = (AttentionKind.CONNECTOR_STUCK_DELETING,)

    def _of(self, connector: Connector, data: ProjectData) -> Iterable[Finding]:
        age = data.now - connector.updated_at
        if not connector.pending_deletion or age < STUCK_DELETING:
            return []
        return [
            _connector_finding(
                AttentionKind.CONNECTOR_STUCK_DELETING,
                connector,
                Severity.WARNING,
                f" is still being deleted after {_age(age)}: its cloud instances have not all shut down.",
            )
        ]


# Every family, in the order they run; the order of the lines is build_report's.
CHECKS: tuple[AttentionCheck, ...] = (
    ConnectorsOffCheck(),
    PoolCheck(),
    CountriesCheck(),
    VendorRefusalCheck(),
    ConnectorFailingCheck(),
    RefusalCheck(),
    ConnectorErrorCheck(),
    NoHealthyProxyCheck(),
    TrafficCheck(),
    ExitLocationCheck(),
    ReputationCheck(),
    StuckDeletingCheck(),
)


@dataclass(frozen=True)
class Report:
    """The lines to show in display order, and the key of every line found, those a worse or folded line stands in for
    included, so an until-clear mute ends only when its line is really gone."""

    findings: list[Finding]
    present: frozenset[str]


def build_report(data: ProjectData, checks: Sequence[AttentionCheck]) -> Report:
    """The checks' findings in display order, those a present finding makes redundant dropped.

    Problems first, then warnings, each by kind, then by name and key: a refresh only changes words in place.
    """
    findings = [finding for check in checks for finding in check.findings(data)]
    redundant: set[str] = set()
    for finding in findings:
        redundant |= finding.suppresses
    kept = [finding for finding in findings if finding.key not in redundant and finding.kind not in redundant]
    ordered = sorted(
        kept,
        key=lambda f: (
            0 if f.severity == Severity.PROBLEM else 1,
            _KIND_ORDER[f.kind],
            f.sort_name.casefold(),
            f.key,
        ),
    )
    return Report(ordered, frozenset(key for finding in findings for key in (finding.key, *finding.covers)))


# --- Helpers --------------------------------------------------------------------------------------


def _takes_requests(connector: Connector) -> bool:
    """Enabled and not being deleted."""
    return connector.enabled and not connector.pending_deletion


def _refusal_key(kind: str) -> str:
    return f"{AttentionKind.REFUSALS.value}:{kind}"


def _connector_finding(
    kind: AttentionKind,
    connector: Connector,
    severity: Severity,
    text: str,
    *,
    detail: str | None = None,
    when: AttentionWhen | None = None,
) -> Finding:
    """A line about one connector, led by its name and opening it."""
    return Finding(
        key=f"{kind.value}:{connector.id}",
        kind=kind,
        severity=severity,
        lead="",
        subject=connector.name,
        text=text,
        link=f"connectors?open={connector.id}",
        action="Open connector",
        detail=detail,
        when=when,
        sort_name=connector.name,
    )


def _pool_finding(
    subject: str, text: str, *, link: str, action: str, suppresses: Collection[str]
) -> Finding:
    """The project's pool takes no request."""
    return Finding(
        key=AttentionKind.POOL_NO_ROUTABLE.value,
        kind=AttentionKind.POOL_NO_ROUTABLE,
        severity=Severity.PROBLEM,
        lead="",
        subject=subject,
        text=text,
        link=link,
        action=action,
        suppresses=frozenset(suppresses),
    )


def _countries(names: Sequence[str]) -> str:
    """Country names as a sentence names them: past three, the first two and how many more."""
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more countries"


def _volume(limit_bytes: int) -> str:
    """A traffic limit in decimal gigabytes, as vendors bill it; terabytes from a thousand."""
    gigabytes = limit_bytes / BYTES_PER_GB
    if gigabytes >= 1000:
        return f"{gigabytes / 1000:g} TB"
    return f"{gigabytes:g} GB"


def _age(age: timedelta) -> str:
    """How long, in its largest whole unit."""
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return _plural(minutes, "minute")
    if minutes < 24 * 60:
        return _plural(minutes // 60, "hour")
    return _plural(minutes // (24 * 60), "day")


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"

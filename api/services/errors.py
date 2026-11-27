# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What a service refuses with, so routes answer the right status.

Each error's message is meant for the user; uniqueness is left to the
database, since only an index holds across processes, and
:func:`name_conflict` translates the ``IntegrityError`` a conflict raises at
commit. None subclasses ``TimeoutError``, ``ConnectionError`` or ``OSError``,
which the routes answer as a store that cannot be reached.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from api.providers.registry import UnknownProviderError
from api.providers.sdk.validation import ConfigValidationError


class NotFoundError(LookupError):
    """The entity a write names does not exist, or belongs to another project (404)."""


class WriteRejectedError(ValueError):
    """The write breaks a rule the stored rows or the request must keep (400)."""


class NameConflictError(WriteRejectedError):
    """A name or username that must be unique is taken by another row (400)."""


class InvalidConfigError(ValueError):
    """A config failed its validator; the message lists what is wrong (422)."""


class InputRejectedError(ValueError):
    """A read refuses an argument that names nothing it can read, such as a site that is a public suffix (400)."""


class ForbiddenError(Exception):
    """The caller may not do this with these arguments, whatever the endpoint lets its role do (403)."""


class ConflictError(Exception):
    """The write cannot apply to the entity as it stands, such as a built-in provider or an id taken (409)."""


class HostConfirmationError(ConflictError):
    """A provider write names egress hosts the caller has not confirmed; the answer lists them beside the message (409)."""

    def __init__(self, egress_hosts: list[str], unconfirmed_hosts: list[str]) -> None:
        super().__init__("Confirm the hosts this provider will send credentials to")
        self.egress_hosts = egress_hosts
        self.unconfirmed_hosts = unconfirmed_hosts


class UpstreamError(Exception):
    """A vendor or a download the request relies on refused or failed; the message says how (502)."""


class TimedOutError(Exception):
    """A read took longer than it may; the message says what to narrow (504)."""


class UnavailableError(Exception):
    """Something the request needs cannot be reached right now; the client is told to try again shortly (503)."""


def name_conflict(exc: IntegrityError, messages: Mapping[str, str]) -> NameConflictError:
    """The conflict a unique index of ``messages`` reports, as the message mapped to that index.

    ``messages`` maps a unique index or constraint name to the message for the
    user; any other integrity error is re-raised unchanged, and callers raise
    the result ``from None`` to keep the SQL in the cause from the user.
    """
    reported = str(exc.orig or exc)
    for index, message in messages.items():
        if f'"{index}"' in reported:
            return NameConflictError(message)
    raise exc


def validated(validate: Callable[[dict[str, Any]], dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """``validate(config)``, with every way a config validator refuses turned into :class:`InvalidConfigError`.

    Validators raise an unknown provider type, the SDK's field errors,
    pydantic's model errors (whose messages are joined) or a plain
    ``ValueError``.
    """
    try:
        return validate(config)
    except (UnknownProviderError, ConfigValidationError) as exc:
        raise InvalidConfigError(str(exc)) from None
    except ValidationError as exc:
        raise InvalidConfigError("; ".join(error.get("msg", str(error)) for error in exc.errors())) from None
    except ValueError as exc:
        raise InvalidConfigError(str(exc)) from None

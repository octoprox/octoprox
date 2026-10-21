# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What a write service refuses with, so routes answer the right status.

Each error's message is meant for the user; uniqueness is left to the
database, since only an index holds across processes, and
:func:`name_conflict` translates the ``IntegrityError`` a conflict raises at
commit.
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

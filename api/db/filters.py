# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Membership filters and row sources that bind a list as arrays, so a list of any length is the same SQL text.

An expanded ``IN`` list or a ``VALUES`` list is new text for every length, and each pooled connection keeps up to a
hundred statements prepared, with their plans, in its Postgres backend.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, String, all_, any_, column, func, literal
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.selectable import TableValuedAlias

type Column = QueryableAttribute[Any] | ColumnElement[Any]


def array_of(column: Column, values: Iterable[Any]) -> ColumnElement[Any]:
    """``values`` as one array parameter of ``column``'s type, a string type without its length.

    Cast to ``varchar(n)[]``, a longer value would be cut to ``n`` characters and match the row it begins with.
    """
    item = column.type
    if isinstance(item, String) and item.length is not None:
        item = String()
    return literal(list(values), ARRAY(item))


def any_of(column: Column, values: Iterable[Any]) -> ColumnElement[bool]:
    """``column`` equal to one of ``values``: ``column = ANY(:values)`` where ``in_`` binds each value."""
    return column == any_(array_of(column, values))


def none_of(column: Column, values: Iterable[Any]) -> ColumnElement[bool]:
    """``column`` equal to none of ``values``: ``column <> ALL(:values)`` where ``not_in`` binds each value."""
    return column != all_(array_of(column, values))


def rows_of(name: str, columns: Mapping[str, Column], rows: Iterable[Sequence[Any]]) -> TableValuedAlias:
    """``rows`` as a table ``name``: one array parameter per column, of the type of the column it names, unnested side by side.

    Stands in for a ``VALUES`` list in a join, an ``UPDATE ... FROM`` or an ``INSERT ... SELECT``.
    """
    rows = list(rows)
    arrays = [array_of(typed, [row[place] for row in rows]) for place, typed in enumerate(columns.values())]
    return (
        func.unnest(*arrays)
        .table_valued(*(column(label, typed.type) for label, typed in columns.items()))
        .render_derived(name=name)
    )

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any


def _json_tuple(values: tuple[Any, ...]) -> str:
    return json.dumps([asdict(value) for value in values], sort_keys=True)


def _json_object(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)


def _json_string_tuple(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), sort_keys=True)


def _count_rows(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0])

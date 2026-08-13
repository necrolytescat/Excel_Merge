from __future__ import annotations

import sqlite3

import pytest

from app.services.batch_store import BatchStore


def test_connection_context_closes_connection(tmp_path):
    store = BatchStore(tmp_path / "state")

    with store._connect() as connection:
        connection.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

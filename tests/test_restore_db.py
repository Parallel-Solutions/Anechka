"""Unit tests for db-restore skip/restore decision logic."""

from __future__ import annotations

import pytest

from scripts.restore_db import should_restore


@pytest.mark.parametrize(
    ("table_count", "crm_count", "seed_exists", "expected"),
    [
        (0, 0, True, True),
        (5, 100, True, False),
        (5, 0, True, True),
        (0, 0, False, False),
        (5, 100, False, False),
    ],
)
def test_should_restore(table_count: int, crm_count: int, seed_exists: bool, expected: bool) -> None:
    assert should_restore(table_count, crm_count, seed_exists) is expected

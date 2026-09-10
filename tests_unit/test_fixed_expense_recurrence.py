from datetime import date
from types import SimpleNamespace

import pytest

from nexasalon_api.models.enums import FixedExpenseRecurrence
from nexasalon_api.services.fixed_expenses import due_date_for, occurs_in


@pytest.mark.parametrize(
    ("recurrence", "included", "excluded"),
    [
        (FixedExpenseRecurrence.MONTHLY, date(2026, 10, 1), None),
        (FixedExpenseRecurrence.QUARTERLY, date(2026, 12, 1), date(2026, 11, 1)),
        (FixedExpenseRecurrence.SEMIANNUAL, date(2027, 3, 1), date(2027, 2, 1)),
        (FixedExpenseRecurrence.ANNUAL, date(2027, 9, 1), date(2027, 8, 1)),
    ],
)
def test_recurrence_preserves_real_competence(recurrence, included, excluded):
    version = SimpleNamespace(
        is_active=True, start_month=date(2026, 9, 1), end_month=None, recurrence=recurrence
    )
    assert occurs_in(version, included)
    if excluded:
        assert not occurs_in(version, excluded)


@pytest.mark.parametrize(
    ("month", "day", "expected"),
    [
        (date(2026, 2, 1), 29, date(2026, 2, 28)),
        (date(2026, 2, 1), 30, date(2026, 2, 28)),
        (date(2026, 2, 1), 31, date(2026, 2, 28)),
        (date(2028, 2, 1), 31, date(2028, 2, 29)),
        (date(2026, 4, 1), 31, date(2026, 4, 30)),
    ],
)
def test_due_day_uses_last_valid_day(month, day, expected):
    assert due_date_for(month, day) == expected


def test_start_end_and_inactive_are_respected():
    version = SimpleNamespace(
        is_active=True,
        start_month=date(2026, 9, 1),
        end_month=date(2026, 11, 1),
        recurrence=FixedExpenseRecurrence.MONTHLY,
    )
    assert not occurs_in(version, date(2026, 8, 1))
    assert occurs_in(version, date(2026, 9, 1))
    assert occurs_in(version, date(2026, 11, 1))
    assert not occurs_in(version, date(2026, 12, 1))
    version.is_active = False
    assert not occurs_in(version, date(2026, 10, 1))

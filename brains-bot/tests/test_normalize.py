from datetime import date, datetime
from decimal import Decimal

import pytest

from brains_bot.importers import normalize as n


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "+254712345678"),
        (712345678, "+254712345678"),  # stored as a number: leading zero lost
        ("0722 123456", "+254722123456"),
        ("+254712345678", "+254712345678"),
        ("254712345678", "+254712345678"),
        ("0110123456", "+254110123456"),
    ],
)
def test_valid_kenyan_numbers(raw, expected):
    result = n.normalize_phone(raw)
    assert result.status == "valid"
    assert result.e164 == expected


@pytest.mark.parametrize(
    "raw",
    ["0712345678 & 0722333444", "0712345678 or 722333444", "0712345678 & 0722333444 resp."],
)
def test_multiple_numbers_are_kept_but_not_chosen(raw):
    result = n.normalize_phone(raw)
    assert result.status == "multiple"
    assert result.e164 is None
    assert result.numbers == ["+254712345678", "+254722333444"]


def test_same_number_twice_counts_once():
    assert n.normalize_phone("0712345678 / +254712345678").status == "valid"


@pytest.mark.parametrize(("raw", "status"), [(None, "missing"), ("TBA", "placeholder"),
                                             ("TBC", "placeholder"), ("12345", "invalid"),
                                             ("0612345678", "invalid")])  # fmt: skip
def test_unusable_numbers(raw, status):
    result = n.normalize_phone(raw)
    assert result.status == status
    assert result.e164 is None


def test_format_phone():
    assert n.format_phone("+254712345678") == "+254 712 345 678"
    assert n.format_phone(None) == "not available"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (datetime(2025, 3, 1, 9, 30), date(2025, 3, 1)),
        ("9/16/2025", date(2025, 9, 16)),  # US month/day/year, as exported by Forms
        ("9/16/2025 11:07", date(2025, 9, 16)),
        (45716.5380208333, date(2025, 2, 28)),  # Excel serial number
        ("15.10.25", date(2025, 10, 15)),  # typed day.month.year
        ("TBC", None),
        (None, None),
    ],
)
def test_parse_date(raw, expected):
    assert n.parse_date(raw)[0] == expected


def test_parse_date_reports_garbage():
    value, issue = n.parse_date("next month")
    assert value is None
    assert "unparseable" in issue


ALIASES = {"Jane": "Jane Wanjiru", "Peter Otieno": "Peter Otieno Ouma"}


@pytest.mark.parametrize(
    ("raw", "name", "alias"),
    [
        ("Jane Wanjiru ", "Jane Wanjiru", None),
        ("Jane - project.", "Jane Wanjiru", "Jane"),
        ("Jane- Project", "Jane Wanjiru", "Jane"),
        ("John Doe (Trainee) -PROJECTS", "John Doe", None),
        ("Peter Otieno, Trainee Project", "Peter Otieno Ouma", "Peter Otieno"),
        ("Peter Otieno Ouma, Trainee -Project ", "Peter Otieno Ouma", None),
    ],
)
def test_normalize_salesperson(raw, name, alias):
    result = n.normalize_salesperson(raw, ALIASES)
    assert (result.name, result.alias_applied) == (name, alias)


def test_salesperson_without_aliases_only_cleans():
    assert n.normalize_salesperson("Jane - project.").name == "Jane"


def test_normalize_city_and_business_names():
    assert n.normalize_city("Nairobi ") == "Nairobi"
    assert n.normalize_city("Edoret") == "Eldoret"
    assert n.normalize_city("City") is None
    assert n.normalize_business_name("Megha marketing") == "Megha Marketing"
    assert n.normalize_business_name("HGS Nairobi") == "HGS Nairobi"
    assert n.normalize_business_name("H&M") == "H&M"


def test_choices_and_amounts():
    assert n.normalize_choice("lost") == "Lost"
    assert n.normalize_choice("N/A") is None
    assert n.parse_amount(450000) == (Decimal(450000), None)
    assert n.parse_amount("1,200,000") == (Decimal(1200000), None)
    assert n.parse_amount("about a million")[0] is None

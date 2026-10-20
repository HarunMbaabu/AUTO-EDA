"""Pure normalisation helpers for workbook values. Originals are always kept elsewhere."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

PLACEHOLDERS = {
    "", "tbc", "tba", "n/a", "na", "none", "nil", "-", "--", "not specified",
    "none specified", "unknown", "city",
}  # fmt: skip

CITY_ALIASES = {"Edoret": "Eldoret"}

EXCEL_EPOCH = datetime(1899, 12, 30)


def clean_text(value: Any) -> str | None:
    """Collapse whitespace; return None for empty values. Does not touch placeholders."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def is_placeholder(value: Any) -> bool:
    text = clean_text(value)
    return text is None or text.lower().strip(" .") in PLACEHOLDERS


def meaningful(value: Any) -> str | None:
    """Cleaned text, or None for blanks and placeholders like TBC / N/A."""
    return None if is_placeholder(value) else clean_text(value)


def name_key(value: str | None) -> str:
    """Lowercase alphanumerics only, for comparing names."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


# --- Phones -------------------------------------------------------------------------------


@dataclass
class PhoneResult:
    raw: str | None
    numbers: list[str] = field(default_factory=list)  # valid E.164 numbers, de-duplicated
    invalid_parts: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.raw is None:
            return "missing"
        if is_placeholder(self.raw):
            return "placeholder"
        if len(self.numbers) == 1 and not self.invalid_parts:
            return "valid"
        if len(self.numbers) > 1:
            return "multiple"
        return "invalid"

    @property
    def e164(self) -> str | None:
        """Only an unambiguous single number is normalised for use."""
        return self.numbers[0] if self.status == "valid" else None


def _normalize_one_phone(part: str) -> str | None:
    digits = re.sub(r"\D", "", part)
    if len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    elif len(digits) == 10 and digits[:2] in ("07", "01"):
        digits = "254" + digits[1:]
    if len(digits) == 12 and digits.startswith(("2547", "2541")):
        return "+" + digits
    return None


def normalize_phone(value: Any) -> PhoneResult:
    raw = clean_text(value)
    result = PhoneResult(raw=raw)
    if raw is None or is_placeholder(raw):
        return result
    for part in re.split(r"\s*(?:&|/|,|;|\bor\b|\band\b)\s*", raw, flags=re.IGNORECASE):
        part = re.sub(r"\b(resp\.?|respectively)\b", "", part, flags=re.IGNORECASE).strip()
        if not part:
            continue
        number = _normalize_one_phone(part)
        if number:
            if number not in result.numbers:
                result.numbers.append(number)
        else:
            result.invalid_parts.append(part)
    return result


# Phone numbers inside free text, in every form normalize_phone accepts: 0712 345 678,
# 712345678, +254 712 345 678, (+254) 712-345-678, 254712345678; plus any 9+ digit run.
_PHONE_IN_TEXT = re.compile(
    r"(?<![\w+])(?:\(?\+?\s?254\)?[\s.-]*|0)?[17](?:[\s.-]?\d){8}(?!\d)|(?<!\d)\d{9,}(?!\d)"
)
_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def redact_contact_details(text: str) -> str:
    """Free text with phone numbers and email addresses replaced; names are kept."""
    return _EMAIL_IN_TEXT.sub("[email]", _PHONE_IN_TEXT.sub("[phone]", text))


def mask_phone(e164: str | None) -> str:
    """+254712345678 -> +254 7XX XXX 678"""
    if not e164 or len(e164) < 7:
        return "not available"
    return f"{e164[:4]} {e164[4]}XX XXX {e164[-3:]}"


# --- Dates --------------------------------------------------------------------------------

_DATE_FORMATS = (
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",  # US style, as exported by Forms
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%d.%m.%y", "%d.%m.%Y",  # typed by hand, e.g. 15.10.25
)  # fmt: skip


def parse_datetime(value: Any) -> tuple[datetime | None, str | None]:
    """Return (value, issue). Text dates in this workbook are US month/day/year."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, datetime):
        return value, None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day), None
    if isinstance(value, int | float) and not isinstance(value, bool):
        if 20000 < value < 80000:  # Excel serial date (1954-2119)
            return EXCEL_EPOCH + timedelta(days=float(value)), None
        return None, f"unrecognised number as date: {value}"
    text = clean_text(value) or ""
    if is_placeholder(text):
        return None, None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, f"unparseable date: {text[:40]}"


def parse_date(value: Any) -> tuple[date | None, str | None]:
    parsed, issue = parse_datetime(value)
    return (parsed.date() if parsed else None), issue


# --- Names, places, amounts ---------------------------------------------------------------


@dataclass
class Salesperson:
    name: str | None
    alias_applied: str | None = None


def normalize_salesperson(value: Any, aliases: Mapping[str, str] | None = None) -> Salesperson:
    """'Jane - project.' -> 'Jane' (then aliases, e.g. 'Jane' -> 'Jane Wanjiru');
    'John Doe (Trainee) -PROJECT' -> 'John Doe'."""
    text = clean_text(value)
    if text is None:
        return Salesperson(None)
    text = re.sub(r"[-,]?\s*\btrainee\b\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[()]", " ", text)
    text = re.sub(r"\s*-?\s*\bprojects?\b\.?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s,.-]+$", "", re.sub(r"\s+", " ", text)).strip()
    name = text.title() if text else None
    if aliases and name in aliases:
        return Salesperson(aliases[name], alias_applied=name)
    return Salesperson(name)


def normalize_city(value: Any) -> str | None:
    text = meaningful(value)
    if text is None:
        return None
    text = text.rstrip(" .")
    text = text if any(c.islower() for c in text[1:]) else text.title()
    return CITY_ALIASES.get(text, text)


def normalize_choice(value: Any) -> str | None:
    """'yes' -> 'Yes', 'lost' -> 'Lost', 'Supllying' kept as-is (reported, not guessed)."""
    text = meaningful(value)
    return text[0].upper() + text[1:] if text else None


def normalize_business_name(value: Any) -> str | None:
    """'Megha marketing' -> 'Megha Marketing'; leaves 'HGS Nairobi' and 'H&M' alone."""
    text = meaningful(value)
    if text is None:
        return None
    return " ".join(w[0].upper() + w[1:] if w.islower() else w for w in text.split(" "))


def parse_amount(value: Any) -> tuple[Decimal | None, str | None]:
    text = clean_text(value)
    if text is None:
        return None, None
    try:
        amount = Decimal(text.replace(",", "").replace("KES", "").replace("Ksh", "").strip())
    except InvalidOperation:
        return None, f"unparseable amount: {text[:30]}"
    return amount, None

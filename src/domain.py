from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

PERSON_TITLES = [
    "CEO",
    "chief executive officer",
    "founder",
    "co-founder",
    "cofounder",
    "owner",
    "CRO",
    "chief revenue officer",
    "VP of sales",
    "vice president of sales",
    "VP sales",
    "head of sales",
    "SVP sales",
    "SVP of sales",
    "sales director",
    "director of sales",
    "director of sales development",
]

PERSON_SENIORITIES = [
    "owner",
    "founder",
    "c_suite",
    "vp",
    "head",
    "director",
]

DEFAULT_BANDS = ["11-50", "1-10", "50+"]

ID_ALIASES = ["id", "company_id", "uuid"]
NAME_ALIASES = ["name", "company_name", "legal_name", "organization_name", "account_name"]
DOMAIN_ALIASES = ["domain", "website_domain", "primary_domain", "normalized_domain", "company_domain"]
WEBSITE_ALIASES = ["website", "website_url", "url", "company_website", "web_url"]
EMPLOYEE_ALIASES = [
    "employee_count",
    "employees",
    "num_employees",
    "estimated_num_employees",
    "company_size",
    "headcount",
    "employee_band",
    "size",
]


@dataclass(frozen=True)
class EmployeeBand:
    key: str
    min_count: int | None
    max_count: int | None

    def contains(self, count: int | None) -> bool:
        if count is None:
            return False
        if self.min_count is not None and count < self.min_count:
            return False
        if self.max_count is not None and count > self.max_count:
            return False
        return True


BAND_DEFINITIONS = {
    "1-10": EmployeeBand("1-10", 1, 10),
    "11-50": EmployeeBand("11-50", 11, 50),
    "50+": EmployeeBand("50+", 51, None),
}


def parse_bands(raw: str | None) -> list[EmployeeBand]:
    if not raw or not raw.strip():
        keys = DEFAULT_BANDS
    else:
        keys = [part.strip() for part in raw.split(",") if part.strip()]
    bands: list[EmployeeBand] = []
    for key in keys:
        band = BAND_DEFINITIONS.get(key)
        if band is None:
            raise SystemExit(
                f"Unknown employee band '{key}'. Use one of: {', '.join(BAND_DEFINITIONS)}"
            )
        bands.append(band)
    return bands


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "@" in text and "://" not in text and "/" not in text:
        text = text.rsplit("@", 1)[-1]
    if "://" not in text:
        text = "https://" + text
    host = urlparse(text).hostname
    if not host:
        return None
    host = host.removeprefix("www.")
    return host or None


def company_domain(domain: str | None, website: str | None) -> str | None:
    return normalize_domain(domain) or normalize_domain(website)


def parse_employee_count(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    text = str(value).strip().lower().replace(",", "").replace(" employees", "")
    if not text:
        return None
    if text.endswith("+"):
        digits = re.sub(r"[^\d]", "", text[:-1])
        return int(digits) if digits else None
    match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", text)
    if match:
        low = int(match.group(1))
        high = int(match.group(2))
        return high if high > 0 else low
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def band_key_for_count(count: int | None) -> str:
    if count is None:
        return "unknown"
    for band in BAND_DEFINITIONS.values():
        if band.contains(count):
            return band.key
    return "unknown"


def first_present(row: dict, candidates: list[str]) -> str | None:
    for name in candidates:
        if name in row:
            return name
    return None

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    apollo_api_key: str
    apollo_api_base_url: str
    apollo_webhook_url: str
    apollo_list_name: str
    companies_table: str
    companies_id_column: str
    companies_name_column: str
    companies_domain_column: str
    companies_website_column: str
    companies_employee_column: str

    @classmethod
    def load(cls, *, require_apollo: bool = True) -> "Settings":
        return cls(
            supabase_url=_require("SUPABASE_URL"),
            supabase_service_role_key=_require("SUPABASE_SERVICE_ROLE_KEY"),
            apollo_api_key=_require("APOLLO_API_KEY") if require_apollo else _optional("APOLLO_API_KEY"),
            apollo_api_base_url=_optional(
                "APOLLO_API_BASE_URL", "https://api.apollo.io/api/v1"
            ).rstrip("/"),
            apollo_webhook_url=_optional("APOLLO_WEBHOOK_URL"),
            apollo_list_name=_optional("APOLLO_LIST_NAME", "TAM Decision Makers"),
            companies_table=_optional("COMPANIES_TABLE", "companies"),
            companies_id_column=_optional("COMPANIES_ID_COLUMN"),
            companies_name_column=_optional("COMPANIES_NAME_COLUMN"),
            companies_domain_column=_optional("COMPANIES_DOMAIN_COLUMN"),
            companies_website_column=_optional("COMPANIES_WEBSITE_COLUMN"),
            companies_employee_column=_optional("COMPANIES_EMPLOYEE_COLUMN"),
        )

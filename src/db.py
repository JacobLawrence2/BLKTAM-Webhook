from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from supabase import Client, create_client

from src.apollo import ApolloPerson
from src.config import Settings
from src.domain import (
    DOMAIN_ALIASES,
    EMPLOYEE_ALIASES,
    ID_ALIASES,
    NAME_ALIASES,
    WEBSITE_ALIASES,
    EmployeeBand,
    band_key_for_count,
    company_domain,
    first_present,
    parse_employee_count,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000

QueryBuilder = Callable[[Any], Any]


@dataclass(frozen=True)
class CompanyColumns:
    table: str
    id_column: str
    name_column: str | None
    domain_column: str | None
    website_column: str | None
    employee_column: str | None
    employee_is_numeric: bool

    def as_dict(self) -> dict[str, str | None | bool]:
        return {
            "table": self.table,
            "id": self.id_column,
            "name": self.name_column,
            "domain": self.domain_column,
            "website": self.website_column,
            "employee_count": self.employee_column,
            "employee_is_numeric": self.employee_is_numeric,
        }


@dataclass(frozen=True)
class Company:
    id: str
    name: str | None
    domain: str | None
    employee_count: int | None
    band: str
    raw: dict[str, Any]


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Client = create_client(
            settings.supabase_url, settings.supabase_service_role_key
        )
        self.columns: CompanyColumns | None = None

    def inspect_companies_schema(self) -> CompanyColumns:
        if self.columns is not None:
            return self.columns
        table = self.settings.companies_table
        sample = self._select(table, "*", limit=20)
        row = next((item for item in sample if item), {})
        available = set(row.keys()) if row else set()

        id_column = self._resolve_column(
            table, self.settings.companies_id_column, ID_ALIASES, available, required=True
        )
        name_column = self._resolve_column(
            table, self.settings.companies_name_column, NAME_ALIASES, available, required=False
        )
        domain_column = self._resolve_column(
            table,
            self.settings.companies_domain_column,
            DOMAIN_ALIASES,
            available,
            required=False,
        )
        website_column = self._resolve_column(
            table,
            self.settings.companies_website_column,
            WEBSITE_ALIASES,
            available,
            required=False,
        )
        employee_column = self._resolve_column(
            table,
            self.settings.companies_employee_column,
            EMPLOYEE_ALIASES,
            available,
            required=False,
        )
        if not domain_column and not website_column:
            raise SystemExit(
                f"Could not find a domain or website column on {table}. "
                "Set COMPANIES_DOMAIN_COLUMN and/or COMPANIES_WEBSITE_COLUMN."
            )

        employee_is_numeric = False
        if employee_column:
            values = [
                item.get(employee_column)
                for item in sample
                if item.get(employee_column) is not None
            ]
            employee_is_numeric = any(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            )

        self.columns = CompanyColumns(
            table=table,
            id_column=id_column,
            name_column=name_column,
            domain_column=domain_column,
            website_column=website_column,
            employee_column=employee_column,
            employee_is_numeric=employee_is_numeric,
        )
        logger.info("Mapped companies columns: %s", self.columns.as_dict())
        return self.columns

    def fetch_companies(
        self,
        *,
        bands: list[EmployeeBand],
        limit: int | None,
        company_ids: list[str] | None = None,
    ) -> list[Company]:
        columns = self.inspect_companies_schema()
        if company_ids:
            rows = self._fetch_by_ids(columns, company_ids)
        else:
            rows = self._paginated_select(
                columns.table,
                self._select_list(columns),
                extra=lambda query: query.order(columns.id_column),
            )

        companies = [company for row in rows if (company := self._to_company(columns, row))]
        if company_ids:
            return companies[:limit] if limit else companies

        if not columns.employee_column:
            logger.warning(
                "No employee-count column on %s; returning companies in table order",
                columns.table,
            )
            return companies[:limit] if limit else companies

        wanted = {band.key for band in bands}
        priority = {band.key: index for index, band in enumerate(bands)}
        ranked = [company for company in companies if company.band in wanted]
        ranked.sort(
            key=lambda company: (
                priority.get(company.band, 99),
                company.employee_count or 10**9,
                company.name or "",
            )
        )
        return ranked[:limit] if limit else ranked

    def existing_apollo_ids(self, company_id: str | None = None) -> set[str]:
        def extra(query: Any) -> Any:
            if company_id:
                return query.eq("company_id", company_id)
            return query

        try:
            rows = self._paginated_select("contacts", "apollo_id", extra=extra)
        except Exception as exc:
            raise SystemExit(
                "Could not read contacts. Apply sql/001_contacts.sql first. "
                f"Original error: {exc}"
            ) from exc
        return {str(row["apollo_id"]) for row in rows if row.get("apollo_id")}

    def upsert_contacts(
        self,
        company_id: str,
        people: list[ApolloPerson],
        *,
        phone_reveal_request_id: str | None = None,
        apollo_contact_ids: dict[str, str] | None = None,
    ) -> int:
        if not people:
            return 0
        rows = []
        contact_ids = apollo_contact_ids or {}
        for person in people:
            row: dict[str, Any] = {
                "company_id": company_id,
                "apollo_id": person.apollo_id,
                "first_name": person.first_name,
                "last_name": person.last_name,
                "full_name": person.full_name,
                "title": person.title,
                "seniority": person.seniority,
                "linkedin_url": person.linkedin_url,
                "email": person.email,
            }
            if person.phone:
                row["phone"] = person.phone
            if phone_reveal_request_id:
                row["phone_reveal_request_id"] = phone_reveal_request_id
            contact_id = contact_ids.get(person.apollo_id)
            if contact_id:
                row["apollo_contact_id"] = contact_id
            rows.append(row)
        try:
            self.client.table("contacts").upsert(rows, on_conflict="apollo_id").execute()
        except Exception:
            if not any("apollo_contact_id" in row for row in rows):
                raise
            logger.warning(
                "Could not store apollo_contact_id; apply sql/002_apollo_contact_id.sql if you want it persisted"
            )
            for row in rows:
                row.pop("apollo_contact_id", None)
            self.client.table("contacts").upsert(rows, on_conflict="apollo_id").execute()
        return len(rows)

    def update_phones(self, phones_by_apollo_id: dict[str, str]) -> int:
        updated = 0
        for apollo_id, phone in phones_by_apollo_id.items():
            if not phone:
                continue
            self.client.table("contacts").update({"phone": phone}).eq(
                "apollo_id", apollo_id
            ).execute()
            updated += 1
        return updated

    def _resolve_column(
        self,
        table: str,
        configured: str,
        aliases: list[str],
        available: set[str],
        *,
        required: bool,
    ) -> str | None:
        if configured:
            self._probe_column(table, configured)
            return configured
        if available:
            match = first_present({name: True for name in available}, aliases)
            if match:
                return match
            lowered = {name.lower(): name for name in available}
            for alias in aliases:
                if alias.lower() in lowered:
                    return lowered[alias.lower()]
        for alias in aliases:
            if self._column_exists(table, alias):
                return alias
        if required:
            raise SystemExit(
                f"Could not find required column on {table}. Tried: {', '.join(aliases)}. "
                "Set the matching COMPANIES_*_COLUMN env var."
            )
        return None

    def _column_exists(self, table: str, column: str) -> bool:
        try:
            self.client.table(table).select(column).limit(1).execute()
            return True
        except Exception:
            return False

    def _probe_column(self, table: str, column: str) -> None:
        try:
            self.client.table(table).select(column).limit(1).execute()
        except Exception as exc:
            raise SystemExit(f"Column {table}.{column} is not selectable: {exc}") from exc

    def _fetch_by_ids(self, columns: CompanyColumns, company_ids: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk_start in range(0, len(company_ids), 100):
            chunk = company_ids[chunk_start : chunk_start + 100]
            response = (
                self.client.table(columns.table)
                .select(self._select_list(columns))
                .in_(columns.id_column, chunk)
                .execute()
            )
            rows.extend(response.data or [])
        return rows

    def _select_list(self, columns: CompanyColumns) -> str:
        names = [
            columns.id_column,
            columns.name_column,
            columns.domain_column,
            columns.website_column,
            columns.employee_column,
        ]
        unique = []
        seen: set[str] = set()
        for name in names:
            if name and name not in seen:
                unique.append(name)
                seen.add(name)
        return ",".join(unique)

    def _to_company(self, columns: CompanyColumns, row: dict[str, Any]) -> Company | None:
        company_id = row.get(columns.id_column)
        if company_id is None:
            return None
        domain_value = row.get(columns.domain_column) if columns.domain_column else None
        website_value = row.get(columns.website_column) if columns.website_column else None
        domain = company_domain(
            str(domain_value) if domain_value else None,
            str(website_value) if website_value else None,
        )
        if not domain:
            logger.debug("Skipping company %s: no usable domain/website", company_id)
            return None
        employee_value = row.get(columns.employee_column) if columns.employee_column else None
        count = parse_employee_count(employee_value)
        name_value = row.get(columns.name_column) if columns.name_column else None
        return Company(
            id=str(company_id),
            name=str(name_value) if name_value else None,
            domain=domain,
            employee_count=count,
            band=band_key_for_count(count),
            raw=row,
        )

    def _select(self, table: str, columns: str, limit: int) -> list[dict[str, Any]]:
        try:
            response = self.client.table(table).select(columns).limit(limit).execute()
        except Exception as exc:
            raise SystemExit(f"Could not read {table}: {exc}") from exc
        return response.data or []

    def _paginated_select(
        self,
        table: str,
        select_list: str,
        *,
        extra: QueryBuilder | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_size = PAGE_SIZE
            if limit is not None:
                remaining = limit - len(rows)
                if remaining <= 0:
                    break
                page_size = min(PAGE_SIZE, remaining)
            query = self.client.table(table).select(select_list)
            if extra:
                query = extra(query)
            response = query.range(offset, offset + page_size - 1).execute()
            batch = response.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return rows

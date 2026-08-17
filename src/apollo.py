from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlencode

import httpx

from src.domain import PERSON_SENIORITIES, PERSON_TITLES

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 100
BULK_MATCH_SIZE = 10


@dataclass(frozen=True)
class ApolloPerson:
    apollo_id: str
    first_name: str | None
    last_name: str | None
    full_name: str | None
    title: str | None
    seniority: str | None
    linkedin_url: str | None
    email: str | None
    phone: str | None


@dataclass(frozen=True)
class BulkEnrichResult:
    people: list[ApolloPerson]
    request_id: str | None
    credits_consumed: float
    missing_records: int


class ApolloError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class ApolloClient:
    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
                "cache-control": "no-cache",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApolloClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def search_people(
        self,
        domain: str,
        *,
        max_people: int | None = None,
        titles: list[str] | None = None,
        seniorities: list[str] | None = None,
    ) -> list[ApolloPerson]:
        people: list[ApolloPerson] = []
        page = 1
        while True:
            payload = {
                "page": page,
                "per_page": SEARCH_PAGE_SIZE,
                "q_organization_domains_list": [domain],
                "person_titles": titles or PERSON_TITLES,
                "person_seniorities": seniorities or PERSON_SENIORITIES,
                "include_similar_titles": True,
            }
            data = self._request("POST", "/mixed_people/api_search", json=payload)
            batch = _people_from_search(data)
            if not batch:
                break
            people.extend(batch)
            if max_people is not None and len(people) >= max_people:
                return _dedupe_people(people[:max_people])
            pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
            total_pages = int(pagination.get("total_pages") or page)
            if page >= total_pages or len(batch) < SEARCH_PAGE_SIZE:
                break
            page += 1
        return _dedupe_people(people)

    def bulk_enrich(
        self,
        apollo_ids: list[str],
        *,
        reveal_phone: bool = False,
        webhook_url: str | None = None,
    ) -> BulkEnrichResult:
        people: list[ApolloPerson] = []
        request_ids: list[str] = []
        credits = 0.0
        missing = 0
        for chunk in _chunks(apollo_ids, BULK_MATCH_SIZE):
            query: dict[str, str] = {"reveal_personal_emails": "false"}
            if reveal_phone:
                if not webhook_url:
                    raise ApolloError("--reveal-phone requires APOLLO_WEBHOOK_URL")
                query["reveal_phone_number"] = "true"
                query["webhook_url"] = webhook_url
            body = {"details": [{"id": person_id} for person_id in chunk]}
            data = self._request("POST", "/people/bulk_match", params=query, json=body)
            result = _people_from_enrich(data)
            people.extend(result.people)
            if result.request_id:
                request_ids.append(result.request_id)
            credits += result.credits_consumed
            missing += result.missing_records
        return BulkEnrichResult(
            people=_dedupe_people(people),
            request_id=request_ids[0] if len(request_ids) == 1 else None,
            credits_consumed=credits,
            missing_records=missing,
        )

    def bulk_enrich_chunks(
        self,
        apollo_ids: list[str],
        *,
        reveal_phone: bool = False,
        webhook_url: str | None = None,
    ) -> list[BulkEnrichResult]:
        """Enrich in batches of 10, returning one result per Apollo request.

        Phone webhooks are keyed by request_id, so callers need each chunk's id.
        """
        results: list[BulkEnrichResult] = []
        for chunk in _chunks(apollo_ids, BULK_MATCH_SIZE):
            query: dict[str, str] = {"reveal_personal_emails": "false"}
            if reveal_phone:
                if not webhook_url:
                    raise ApolloError("--reveal-phone requires APOLLO_WEBHOOK_URL")
                query["reveal_phone_number"] = "true"
                query["webhook_url"] = webhook_url
            body = {"details": [{"id": person_id} for person_id in chunk]}
            data = self._request("POST", "/people/bulk_match", params=query, json=body)
            results.append(_people_from_enrich(data))
        return results

    def poll_webhook_result(
        self,
        request_id: str,
        *,
        timeout_seconds: float = 180.0,
        max_interval: float = 20.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                data = self._request(
                    "GET",
                    f"/webhook_result/{request_id}",
                    retry_on_pending=False,
                )
                return data
            except ApolloError as exc:
                error_code = str(exc.payload.get("error_code") or "")
                if error_code in {"request_id_unknown", "request_id_expired", "invalid_request_id"}:
                    logger.warning("Phone reveal %s is not retrievable: %s", request_id, error_code)
                    return None
                if exc.status_code == 404 or error_code == "result_pending":
                    wait = float(exc.payload.get("retry_after_seconds") or 8)
                    wait = min(max_interval, max(3.0, wait), max(1.0, remaining))
                    logger.info("Phone reveal %s still pending; waiting %.0fs", request_id, wait)
                    time.sleep(wait)
                    continue
                if exc.status_code in {400, 410}:
                    logger.warning("Phone reveal %s is not retrievable: %s", request_id, exc)
                    return None
                raise
        logger.warning("Timed out waiting for phone reveal %s", request_id)
        return None

    def save_people_to_list(
        self,
        people: list[ApolloPerson],
        *,
        list_name: str,
        organization_name: str | None = None,
        domain: str | None = None,
    ) -> dict[str, str]:
        """Save enriched people as Apollo contacts and add them to a people list.

        Returns apollo person id → Apollo contact id. 0 credits.
        """
        if not people or not list_name.strip():
            return {}
        mapped: dict[str, str] = {}
        contact_ids: list[str] = []
        for chunk in _chunks(people, 100):
            chunk_map, chunk_ids = self._bulk_create_contacts(
                chunk,
                organization_name=organization_name,
                domain=domain,
                label_names=[list_name],
            )
            mapped.update(chunk_map)
            contact_ids.extend(chunk_ids)
        contact_ids = list(dict.fromkeys(contact_ids))
        if contact_ids:
            self.add_contacts_to_list(contact_ids, list_name)
        unmapped = [person.apollo_id for person in people if person.apollo_id not in mapped]
        if unmapped:
            logger.warning(
                "Could not map %s enriched people to Apollo contacts for list '%s'",
                len(unmapped),
                list_name,
            )
        return mapped

    def add_contacts_to_list(self, contact_ids: list[str], list_name: str) -> None:
        for chunk in _chunks(contact_ids, 100):
            data = self._request(
                "POST",
                "/labels/add_entity_ids_to_label_names",
                json={
                    "entity_ids": chunk,
                    "label_names": [list_name],
                    "modality": "contacts",
                },
            )
            labels = data.get("labels") if isinstance(data.get("labels"), list) else []
            names = [str(item.get("name")) for item in labels if isinstance(item, dict)]
            logger.info(
                "Added %s contacts to Apollo list '%s'%s",
                len(chunk),
                list_name,
                f" ({', '.join(names)})" if names else "",
            )

    def _bulk_create_contacts(
        self,
        people: list[ApolloPerson],
        *,
        organization_name: str | None,
        domain: str | None,
        label_names: list[str],
    ) -> tuple[dict[str, str], list[str]]:
        contacts = [_contact_payload(person, organization_name, domain) for person in people]
        contacts = [item for item in contacts if item]
        if not contacts:
            return {}, []
        data = self._request(
            "POST",
            "/contacts/bulk_create",
            json={
                "contacts": contacts,
                "append_label_names": label_names,
                "run_dedupe": True,
            },
        )
        return _map_bulk_create_ids(data, people), _contact_ids_from_bulk_create(data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        retry_on_pending: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        last_error: ApolloError | None = None
        for attempt in range(6):
            response = self._client.request(method, url, json=json)
            if response.status_code == 429:
                wait = _retry_after(response, default=2 ** attempt)
                logger.warning("Apollo rate limited; sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                wait = 2 ** attempt
                logger.warning("Apollo %s; sleeping %.1fs", response.status_code, wait)
                time.sleep(wait)
                continue
            if response.status_code == 404 and retry_on_pending:
                payload = _safe_json(response)
                if str(payload.get("error_code") or "") == "result_pending":
                    wait = float(payload.get("retry_after_seconds") or 8)
                    time.sleep(wait)
                    continue
            if response.status_code >= 400:
                payload = _safe_json(response)
                message = payload.get("error") or payload.get("message") or response.text
                last_error = ApolloError(
                    f"Apollo {method} {path} failed ({response.status_code}): {message}",
                    status_code=response.status_code,
                    payload=payload,
                )
                if response.status_code in {401, 403}:
                    raise last_error
                raise last_error
            data = _safe_json(response)
            if not isinstance(data, dict):
                raise ApolloError(f"Apollo {method} {path} returned a non-object payload")
            return data
        raise last_error or ApolloError(f"Apollo {method} {path} failed after retries")


def _contact_payload(
    person: ApolloPerson,
    organization_name: str | None,
    domain: str | None,
) -> dict[str, Any] | None:
    first = person.first_name
    last = person.last_name
    if not first and person.full_name:
        parts = person.full_name.split(None, 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else None
    if not first and not last and not person.email:
        return None
    payload: dict[str, Any] = {"person_id": person.apollo_id}
    if first:
        payload["first_name"] = first
    if last:
        payload["last_name"] = last
    if person.email:
        payload["email"] = person.email
    if person.title:
        payload["title"] = person.title
    if person.linkedin_url:
        payload["linkedin_url"] = person.linkedin_url
    if person.phone:
        payload["phone"] = person.phone
    if organization_name:
        payload["organization_name"] = organization_name
    if domain:
        payload["website_url"] = f"https://{domain}"
    return payload


def _map_bulk_create_ids(data: dict[str, Any], people: list[ApolloPerson]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    by_person_id: dict[str, str] = {}
    by_email: dict[str, str] = {}
    rows: list[Any] = []
    for key in ("created_contacts", "existing_contacts"):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(value)
    for row in rows:
        if not isinstance(row, dict):
            continue
        contact_id = _str(row.get("id"))
        if not contact_id:
            continue
        person_id = _str(row.get("person_id"))
        if person_id:
            by_person_id[person_id] = contact_id
        email = _str(row.get("email"))
        if email:
            by_email[email.lower()] = contact_id
    for person in people:
        if person.apollo_id in by_person_id:
            mapped[person.apollo_id] = by_person_id[person.apollo_id]
        elif person.email and person.email.lower() in by_email:
            mapped[person.apollo_id] = by_email[person.email.lower()]
    return mapped


def _contact_ids_from_bulk_create(data: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for key in ("created_contacts", "existing_contacts"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            contact_id = _str(row.get("id"))
            if contact_id and contact_id not in seen:
                seen.add(contact_id)
                ids.append(contact_id)
    return ids


def phones_from_webhook(payload: dict[str, Any]) -> dict[str, str]:
    """Map apollo person id → best phone number from a webhook / poll payload."""
    result = payload.get("webhook_result")
    root = result if isinstance(result, dict) else payload
    people = root.get("people")
    if not isinstance(people, list):
        people = payload.get("people") if isinstance(payload.get("people"), list) else []
    mapped: dict[str, str] = {}
    for person in people:
        if not isinstance(person, dict):
            continue
        person_id = str(person.get("id") or person.get("person_id") or "")
        phone = extract_phone(person)
        if person_id and phone:
            mapped[person_id] = phone
    return mapped


def extract_phone(person: dict[str, Any]) -> str | None:
    for key in ("sanitized_phone", "phone_number", "mobile_phone", "direct_dial"):
        value = person.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    numbers = person.get("phone_numbers")
    if isinstance(numbers, list):
        preferred: list[str] = []
        fallback: list[str] = []
        for item in numbers:
            if not isinstance(item, dict):
                continue
            number = (
                item.get("sanitized_number")
                or item.get("raw_number")
                or item.get("number")
            )
            if not isinstance(number, str) or not number.strip():
                continue
            kind = str(item.get("type_cd") or item.get("type") or "").lower()
            if "mobile" in kind:
                preferred.append(number.strip())
            else:
                fallback.append(number.strip())
        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
    return None


def _people_from_search(data: dict[str, Any]) -> list[ApolloPerson]:
    rows: list[Any] = []
    for key in ("people", "contacts"):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(value)
    return [person for row in rows if (person := _person_from_payload(row))]


def _people_from_enrich(data: dict[str, Any]) -> BulkEnrichResult:
    matches = data.get("matches")
    if not isinstance(matches, list):
        matches = data.get("people") if isinstance(data.get("people"), list) else []
    people = [person for row in matches if (person := _person_from_payload(row))]
    request_id = data.get("request_id")
    credits = data.get("credits_consumed") or 0
    missing = data.get("missing_records") or 0
    try:
        credits_consumed = float(credits)
    except (TypeError, ValueError):
        credits_consumed = 0.0
    try:
        missing_records = int(missing)
    except (TypeError, ValueError):
        missing_records = 0
    return BulkEnrichResult(
        people=people,
        request_id=str(request_id) if request_id is not None else None,
        credits_consumed=credits_consumed,
        missing_records=missing_records,
    )


def _person_from_payload(row: Any) -> ApolloPerson | None:
    if not isinstance(row, dict):
        return None
    person = row.get("person") if isinstance(row.get("person"), dict) else row
    apollo_id = person.get("id") or person.get("person_id") or row.get("id")
    if not apollo_id:
        return None
    first = _str(person.get("first_name"))
    last = _str(person.get("last_name"))
    full = _str(person.get("name") or person.get("full_name"))
    if not full:
        full = " ".join(part for part in (first, last) if part) or None
    return ApolloPerson(
        apollo_id=str(apollo_id),
        first_name=first,
        last_name=last,
        full_name=full,
        title=_str(person.get("title")),
        seniority=_str(person.get("seniority")),
        linkedin_url=_str(person.get("linkedin_url")),
        email=_str(person.get("email") or person.get("work_email")),
        phone=extract_phone(person),
    )


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedupe_people(people: list[ApolloPerson]) -> list[ApolloPerson]:
    seen: set[str] = set()
    unique: list[ApolloPerson] = []
    for person in people:
        if person.apollo_id in seen:
            continue
        seen.add(person.apollo_id)
        unique.append(person)
    return unique


T = TypeVar("T")


def _chunks(values: list[T], size: int) -> list[list[T]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _retry_after(response: httpx.Response, default: float) -> float:
    header = response.headers.get("retry-after")
    if not header:
        return max(1.0, default)
    try:
        return max(1.0, float(header))
    except ValueError:
        return max(1.0, default)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

from __future__ import annotations

import argparse
import json
import logging
import sys

from src.apollo import ApolloClient, ApolloPerson, phones_from_webhook
from src.config import Settings
from src.db import Company, Database
from src.domain import DEFAULT_BANDS, parse_bands

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull companies from Supabase and enrich decision makers via Apollo."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max companies to process this run.",
    )
    parser.add_argument(
        "--bands",
        default=",".join(DEFAULT_BANDS),
        help="Employee-size priority, comma-separated. Default: 11-50,1-10,50+",
    )
    parser.add_argument(
        "--company-id",
        action="append",
        dest="company_ids",
        default=None,
        help="Process this company id (repeatable). Skips band ranking.",
    )
    parser.add_argument(
        "--max-per-company",
        type=int,
        default=0,
        help="Safety cap on people enriched per company. 0 = no cap (all matches).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search Apollo but do not enrich or write contacts (0 enrichment credits).",
    )
    parser.add_argument(
        "--reveal-phone",
        action="store_true",
        help="Reveal mobile phones (+8 credits when returned). Requires APOLLO_WEBHOOK_URL.",
    )
    parser.add_argument(
        "--phone-wait-seconds",
        type=float,
        default=180.0,
        help="How long to poll Apollo for phone webhook results (default 180).",
    )
    parser.add_argument(
        "--apollo-list",
        default=None,
        help="Apollo people list name. Default: APOLLO_LIST_NAME or 'TAM Decision Makers'.",
    )
    parser.add_argument(
        "--skip-apollo-list",
        action="store_true",
        help="Write to Supabase only; do not save contacts onto an Apollo list.",
    )
    parser.add_argument(
        "--inspect-schema",
        action="store_true",
        help="Print detected companies table columns and exit.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    settings = Settings.load(require_apollo=not args.inspect_schema)
    db = Database(settings)
    columns = db.inspect_companies_schema()

    if args.inspect_schema:
        print(json.dumps(columns.as_dict(), indent=2, default=str))
        return 0

    if args.reveal_phone and not settings.apollo_webhook_url:
        raise SystemExit("--reveal-phone requires APOLLO_WEBHOOK_URL in the environment")

    list_name = None if args.skip_apollo_list else (args.apollo_list or settings.apollo_list_name)
    if list_name:
        logger.info("Enriched contacts will be added to Apollo list '%s'", list_name)

    bands = parse_bands(args.bands)
    companies = db.fetch_companies(
        bands=bands,
        limit=args.limit,
        company_ids=args.company_ids,
    )
    if not companies:
        logger.warning("No matching companies found")
        return 0

    logger.info("Loaded %s companies", len(companies))
    existing = db.existing_apollo_ids()
    max_per_company = args.max_per_company if args.max_per_company > 0 else None
    pending_phone_requests: list[tuple[str, list[str]]] = []
    stats = {
        "companies": 0,
        "skipped_no_domain": 0,
        "searched": 0,
        "already_saved": 0,
        "to_enrich": 0,
        "written": 0,
        "listed": 0,
        "credits": 0.0,
    }

    with ApolloClient(settings.apollo_api_key, settings.apollo_api_base_url) as apollo:
        for company in companies:
            stats["companies"] += 1
            result = _process_company(
                company,
                apollo=apollo,
                db=db,
                existing=existing,
                max_per_company=max_per_company,
                dry_run=args.dry_run,
                reveal_phone=args.reveal_phone,
                webhook_url=settings.apollo_webhook_url or None,
                list_name=list_name,
            )
            stats["skipped_no_domain"] += result.get("skipped_no_domain", 0)
            stats["searched"] += result["searched"]
            stats["already_saved"] += result["already_saved"]
            stats["to_enrich"] += result["to_enrich"]
            stats["written"] += result["written"]
            stats["listed"] += result.get("listed", 0)
            stats["credits"] += result["credits"]
            pending_phone_requests.extend(result["phone_requests"])

        if args.reveal_phone and pending_phone_requests and not args.dry_run:
            _collect_phones(
                apollo,
                db,
                pending_phone_requests,
                wait_seconds=args.phone_wait_seconds,
            )

    logger.info(
        "Done. companies=%s searched=%s already_saved=%s to_enrich=%s written=%s listed=%s credits=%.2f",
        stats["companies"],
        stats["searched"],
        stats["already_saved"],
        stats["to_enrich"],
        stats["written"],
        stats["listed"],
        stats["credits"],
    )
    return 0


def _process_company(
    company: Company,
    *,
    apollo: ApolloClient,
    db: Database,
    existing: set[str],
    max_per_company: int | None,
    dry_run: bool,
    reveal_phone: bool,
    webhook_url: str | None,
    list_name: str | None,
) -> dict:
    empty = {
        "searched": 0,
        "already_saved": 0,
        "to_enrich": 0,
        "written": 0,
        "listed": 0,
        "credits": 0.0,
        "phone_requests": [],
        "skipped_no_domain": 0,
    }
    if not company.domain:
        logger.warning("Skipping %s (%s): no domain", company.id, company.name)
        empty["skipped_no_domain"] = 1
        return empty

    logger.info(
        "Company %s (%s) domain=%s band=%s employees=%s",
        company.id,
        company.name or "unnamed",
        company.domain,
        company.band,
        company.employee_count,
    )
    people = apollo.search_people(company.domain, max_people=max_per_company)
    if max_per_company:
        people = people[:max_per_company]
    searched = len(people)
    new_people = [person for person in people if person.apollo_id not in existing]
    already = searched - len(new_people)
    logger.info(
        "  search hits=%s new=%s already_saved=%s",
        searched,
        len(new_people),
        already,
    )

    result = {
        "searched": searched,
        "already_saved": already,
        "to_enrich": len(new_people),
        "written": 0,
        "listed": 0,
        "credits": 0.0,
        "phone_requests": [],
        "skipped_no_domain": 0,
    }
    if dry_run:
        for person in new_people:
            logger.info(
                "  dry-run %s | %s | %s",
                person.full_name or person.apollo_id,
                person.title or "",
                person.linkedin_url or "",
            )
        return result
    if not new_people:
        return result

    written = 0
    credits = 0.0
    listed = 0
    phone_requests: list[tuple[str, list[str]]] = []
    all_merged: list[ApolloPerson] = []
    chunks = apollo.bulk_enrich_chunks(
        [person.apollo_id for person in new_people],
        reveal_phone=reveal_phone,
        webhook_url=webhook_url,
    )
    for chunk in chunks:
        merged = _prefer_enriched(new_people, chunk.people)
        db.upsert_contacts(
            company.id,
            merged,
            phone_reveal_request_id=chunk.request_id if reveal_phone else None,
        )
        for person in merged:
            existing.add(person.apollo_id)
        all_merged.extend(merged)
        written += len(merged)
        credits += chunk.credits_consumed
        if reveal_phone and chunk.request_id:
            phone_requests.append((chunk.request_id, [person.apollo_id for person in merged]))

    if list_name and all_merged:
        try:
            contact_ids = apollo.save_people_to_list(
                all_merged,
                list_name=list_name,
                organization_name=company.name,
                domain=company.domain,
            )
            listed = len(contact_ids)
            if contact_ids:
                db.upsert_contacts(company.id, all_merged, apollo_contact_ids=contact_ids)
        except Exception as exc:
            logger.warning("Failed to add %s contacts to Apollo list '%s': %s", written, list_name, exc)

    result["written"] = written
    result["listed"] = listed
    result["credits"] = credits
    result["phone_requests"] = phone_requests
    logger.info(
        "  wrote %s contacts, listed %s (%.2f credits this company)",
        written,
        listed,
        credits,
    )
    return result


def _prefer_enriched(
    searched: list[ApolloPerson], enriched: list[ApolloPerson]
) -> list[ApolloPerson]:
    by_id = {person.apollo_id: person for person in searched}
    merged: list[ApolloPerson] = []
    seen: set[str] = set()
    for person in enriched:
        base = by_id.get(person.apollo_id)
        if base is None:
            merged.append(person)
        else:
            merged.append(
                ApolloPerson(
                    apollo_id=person.apollo_id,
                    first_name=person.first_name or base.first_name,
                    last_name=person.last_name or base.last_name,
                    full_name=person.full_name or base.full_name,
                    title=person.title or base.title,
                    seniority=person.seniority or base.seniority,
                    linkedin_url=person.linkedin_url or base.linkedin_url,
                    email=person.email or base.email,
                    phone=person.phone or base.phone,
                )
            )
        seen.add(person.apollo_id)
    return merged


def _collect_phones(
    apollo: ApolloClient,
    db: Database,
    pending: list[tuple[str, list[str]]],
    *,
    wait_seconds: float,
) -> None:
    logger.info("Waiting up to %.0fs for %s phone reveal request(s)", wait_seconds, len(pending))
    seen_ids: set[str] = set()
    for request_id, _apollo_ids in pending:
        if request_id in seen_ids:
            continue
        seen_ids.add(request_id)
        payload = apollo.poll_webhook_result(request_id, timeout_seconds=wait_seconds)
        if not payload:
            logger.warning(
                "No phone payload for request %s; contacts still have apollo_id for a later backfill",
                request_id,
            )
            continue
        phones = phones_from_webhook(payload)
        if not phones:
            logger.info("Phone request %s returned no numbers", request_id)
            continue
        updated = db.update_phones(phones)
        logger.info("Updated %s phone numbers from request %s", updated, request_id)


if __name__ == "__main__":
    sys.exit(main())

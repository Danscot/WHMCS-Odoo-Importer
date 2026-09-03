#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WHMCS Connection & Data Shape Test
===================================

Stage-1 validation script.  Run this BEFORE wiring the API fetcher into
the Odoo import flow.  It verifies:

    1. Network reachability and credential validity (ping)
    2. Clients endpoint — field presence & shape
    3. Invoices endpoint — field presence & shape
    4. Transactions endpoint — field presence & shape
    5. Parser compatibility — confirms the fetched dicts feed into the
       existing WhmcsParser normalizers without errors

Usage (standalone, outside Odoo)::

    python3 test_connection.py \
        --url   https://your-whmcs.example.com \
        --id    YOUR_API_IDENTIFIER \
        --secret YOUR_API_SECRET

Usage with legacy credentials::

    python3 test_connection.py \
        --url      https://your-whmcs.example.com \
        --username admin_user \
        --password plain_text_password

Optional flags::

    --no-verify-ssl   Skip TLS certificate verification (dev/staging only)
    --limit N         Records to fetch per endpoint during the test (default 5)

Exit codes::
    0  All checks passed
    1  One or more checks failed (details printed to stdout)
"""

import argparse
import json
import sys
import os
import traceback
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Allow running from inside the module directory without an installed package
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_ROOT = os.path.dirname(_HERE)
if _MODULE_ROOT not in sys.path:
    sys.path.insert(0, _MODULE_ROOT)
# Also allow running from the module root itself
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from whmcs_connector.env_loader import env_bool, env_timeout, load_dotenv
from whmcs_connector.whmcs_api_client import WhmcsApiClient, WhmcsApiError
from whmcs_connector.whmcs_data_fetcher import WhmcsDataFetcher

load_dotenv()

# ---------------------------------------------------------------------------
# Parser compatibility shim
# We import the normalizer/parser only for field-shape verification.
# The parser lives in services/ and has no Odoo ORM dependency, so it
# can be imported and exercised here without an Odoo environment.
# ---------------------------------------------------------------------------
try:
    from services.normalizer import (
        NormalizedClient,
        NormalizedInvoice,
        NormalizedInvoiceLine,
        NormalizedTransaction,
    )
    from services.whmcs_parser import WhmcsParser
    _PARSER_AVAILABLE = True
except ImportError:
    _PARSER_AVAILABLE = False


# ============================================================================
# Colour output helpers (gracefully degrades on Windows)
# ============================================================================

def _supports_colour() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_CLR = _supports_colour()
_GREEN  = "\033[92m" if _CLR else ""
_RED    = "\033[91m" if _CLR else ""
_YELLOW = "\033[93m" if _CLR else ""
_BLUE   = "\033[94m" if _CLR else ""
_BOLD   = "\033[1m"  if _CLR else ""
_RESET  = "\033[0m"  if _CLR else ""

OK      = f"{_GREEN}✓ PASS{_RESET}"
FAIL    = f"{_RED}✗ FAIL{_RESET}"
WARN    = f"{_YELLOW}⚠ WARN{_RESET}"
SKIP    = f"{_BLUE}– SKIP{_RESET}"


def _header(title: str) -> None:
    bar = "─" * 60
    print(f"\n{_BOLD}{bar}{_RESET}")
    print(f"{_BOLD}  {title}{_RESET}")
    print(f"{_BOLD}{bar}{_RESET}")


def _result(label: str, status: str, detail: str = "") -> None:
    line = f"  {status}  {label}"
    if detail:
        line += f"\n         {_YELLOW}{detail}{_RESET}"
    print(line)


# ============================================================================
# Field definitions — what the Odoo importer MUST have per entity type
# ============================================================================

# Fields that the normalizer reads (at least one alias per group).
# If any of these are missing/None/empty on every record we flag a warning.
_CLIENT_REQUIRED_FIELDS = [
    "id",           # NormalizedClient.whmcs_id
    "firstname",    # .first_name
    "lastname",     # .last_name
    "email",        # .email
]
_CLIENT_OPTIONAL_FIELDS = [
    "companyname", "phonenumber", "address1", "city",
    "country", "postcode", "state", "language", "tax_id", "currency",
]

_INVOICE_REQUIRED_FIELDS = [
    "id",       # NormalizedInvoice.whmcs_invoice_id
    "userid",   # .whmcs_client_id
    "total",    # .total
    "status",   # .status
    "date",     # .date
]
_INVOICE_OPTIONAL_FIELDS = [
    "invoicenum", "duedate", "currency", "paymentmethod",
    "subtotal", "tax", "notes", "datepaid",
]

_TRANSACTION_REQUIRED_FIELDS = [
    "id",        # NormalizedTransaction.whmcs_transaction_id
    "userid",    # .whmcs_client_id
    "amountin",  # source of .amount_in
    "date",      # .date
]
_TRANSACTION_OPTIONAL_FIELDS = [
    "invoiceid", "transid", "amountout", "fees",
    "gateway", "paymentmethod", "currency", "description",
]


# ============================================================================
# Individual check functions
# ============================================================================

def check_ping(client: WhmcsApiClient) -> bool:
    _header("CHECK 1 — Connectivity & Authentication")
    try:
        client.ping()
        _result("Reached WHMCS API endpoint", OK)
        _result("Credentials accepted (result=success)", OK)
        return True
    except WhmcsApiError as exc:
        _result("WHMCS API ping", FAIL, str(exc))
        return False
    except Exception as exc:
        _result("Unexpected error during ping", FAIL, traceback.format_exc(limit=3))
        return False


def _check_fields(
    records: List[Dict],
    required: List[str],
    optional: List[str],
    entity: str,
) -> List[str]:
    """
    Verify required fields are present in at least one record.
    Returns a list of problem strings (empty = all good).
    """
    problems = []
    if not records:
        return [f"No {entity} records returned — cannot verify fields."]

    # Collect all keys present across the sample
    all_keys = set()
    for r in records:
        all_keys.update(r.keys())

    for field in required:
        if field not in all_keys:
            problems.append(f"Required field '{field}' not found in any {entity} record.")

    missing_optional = [f for f in optional if f not in all_keys]
    if missing_optional:
        # Optional fields absent is informational, not a failure
        problems.append(
            f"Optional fields absent (may be in detail endpoint): "
            + ", ".join(missing_optional)
        )

    return problems


def check_clients(fetcher: WhmcsDataFetcher, limit: int) -> bool:
    _header("CHECK 2 — Clients Endpoint (GetClients)")
    try:
        clients = fetcher.fetch_clients(limit=limit)
    except WhmcsApiError as exc:
        _result("GetClients API call", FAIL, str(exc))
        return False

    _result(f"GetClients returned {len(clients)} record(s)", OK if clients else WARN)

    if not clients:
        _result("Field verification", SKIP, "No records to inspect.")
        return True  # Not a failure — might be an empty WHMCS installation

    problems = _check_fields(
        clients, _CLIENT_REQUIRED_FIELDS, _CLIENT_OPTIONAL_FIELDS, "client"
    )
    _show_field_report(
        clients[0], _CLIENT_REQUIRED_FIELDS, _CLIENT_OPTIONAL_FIELDS, "Client sample"
    )
    _check_data_quality(clients, "client")

    hard_failures = [p for p in problems if "Optional" not in p]
    soft_warnings = [p for p in problems if "Optional" in p]

    if hard_failures:
        for p in hard_failures:
            _result("Field check", FAIL, p)
        return False

    for p in soft_warnings:
        _result("Field note", WARN, p)

    # Parser compatibility check
    if _PARSER_AVAILABLE:
        ok, msg = _verify_parser_client(clients[0])
        _result("Parser compatibility (NormalizedClient)", OK if ok else FAIL, msg)
        return ok
    else:
        _result("Parser compatibility", SKIP, "services/ not importable outside Odoo — skipped.")
        return True


def check_invoices(fetcher: WhmcsDataFetcher, limit: int) -> bool:
    _header("CHECK 3 — Invoices Endpoint (GetInvoices)")
    try:
        invoices = fetcher.fetch_invoices(limit=limit)
    except WhmcsApiError as exc:
        _result("GetInvoices API call", FAIL, str(exc))
        return False

    _result(f"GetInvoices returned {len(invoices)} record(s)", OK if invoices else WARN)

    if not invoices:
        _result("Field verification", SKIP, "No records to inspect.")
        return True

    problems = _check_fields(
        invoices, _INVOICE_REQUIRED_FIELDS, _INVOICE_OPTIONAL_FIELDS, "invoice"
    )
    _show_field_report(
        invoices[0], _INVOICE_REQUIRED_FIELDS, _INVOICE_OPTIONAL_FIELDS, "Invoice sample"
    )
    _check_data_quality(invoices, "invoice")

    hard_failures = [p for p in problems if "Optional" not in p]
    soft_warnings = [p for p in problems if "Optional" in p]

    if hard_failures:
        for p in hard_failures:
            _result("Field check", FAIL, p)
        return False

    for p in soft_warnings:
        _result("Field note", WARN, p)

    if _PARSER_AVAILABLE:
        ok, msg = _verify_parser_invoice(invoices[0])
        _result("Parser compatibility (NormalizedInvoice)", OK if ok else FAIL, msg)
        return ok
    else:
        _result("Parser compatibility", SKIP, "services/ not importable outside Odoo — skipped.")
        return True


def check_transactions(fetcher: WhmcsDataFetcher, limit: int) -> bool:
    _header("CHECK 4 — Transactions Endpoint (GetTransactions)")
    try:
        transactions = fetcher.fetch_transactions(limit=limit)
    except WhmcsApiError as exc:
        _result("GetTransactions API call", FAIL, str(exc))
        return False

    _result(
        f"GetTransactions returned {len(transactions)} record(s)",
        OK if transactions else WARN,
    )

    if not transactions:
        _result("Field verification", SKIP, "No records to inspect.")
        return True

    problems = _check_fields(
        transactions,
        _TRANSACTION_REQUIRED_FIELDS,
        _TRANSACTION_OPTIONAL_FIELDS,
        "transaction",
    )
    _show_field_report(
        transactions[0],
        _TRANSACTION_REQUIRED_FIELDS,
        _TRANSACTION_OPTIONAL_FIELDS,
        "Transaction sample",
    )
    _check_data_quality(transactions, "transaction")

    hard_failures = [p for p in problems if "Optional" not in p]
    soft_warnings = [p for p in problems if "Optional" in p]

    if hard_failures:
        for p in hard_failures:
            _result("Field check", FAIL, p)
        return False

    for p in soft_warnings:
        _result("Field note", WARN, p)

    if _PARSER_AVAILABLE:
        ok, msg = _verify_parser_transaction(transactions[0])
        _result("Parser compatibility (NormalizedTransaction)", OK if ok else FAIL, msg)
        return ok
    else:
        _result("Parser compatibility", SKIP, "services/ not importable outside Odoo — skipped.")
        return True


# ============================================================================
# Parser compatibility verifiers
# ============================================================================

def _verify_parser_client(raw: dict):
    """Feed one raw client dict through WhmcsParser._parse_client()."""
    try:
        parser = WhmcsParser()
        nc: NormalizedClient = parser._parse_client(raw)
        if not nc.whmcs_id:
            return False, f"whmcs_id came back falsy: {nc.whmcs_id!r}"
        return True, (
            f"whmcs_id={nc.whmcs_id}  name='{nc.display_name}'  "
            f"email='{nc.email}'"
        )
    except Exception as exc:
        return False, f"Parser raised {type(exc).__name__}: {exc}"


def _verify_parser_invoice(raw: dict):
    """Feed one raw invoice dict through WhmcsParser._parse_invoice()."""
    try:
        parser = WhmcsParser()
        ni: NormalizedInvoice = parser._parse_invoice(raw)
        if not ni.whmcs_invoice_id:
            return False, f"whmcs_invoice_id came back falsy: {ni.whmcs_invoice_id!r}"
        return True, (
            f"whmcs_invoice_id={ni.whmcs_invoice_id}  "
            f"client_id={ni.whmcs_client_id}  "
            f"status='{ni.status}'  total={ni.total}  "
            f"currency='{ni.currency}'"
        )
    except Exception as exc:
        return False, f"Parser raised {type(exc).__name__}: {exc}"


def _verify_parser_transaction(raw: dict):
    """Feed one raw transaction dict through WhmcsParser._parse_transaction()."""
    try:
        parser = WhmcsParser()
        nt: NormalizedTransaction = parser._parse_transaction(raw)
        if not nt.whmcs_transaction_id:
            return False, f"whmcs_transaction_id came back falsy: {nt.whmcs_transaction_id!r}"
        return True, (
            f"whmcs_transaction_id={nt.whmcs_transaction_id}  "
            f"ref='{nt.transaction_reference}'  "
            f"amount_in={nt.amount_in}  amount_out={nt.amount_out}  "
            f"net={nt.amount}  gateway='{nt.gateway}'"
        )
    except Exception as exc:
        return False, f"Parser raised {type(exc).__name__}: {exc}"


# ============================================================================
# Pretty field report
# ============================================================================

def _show_field_report(
    sample: dict,
    required: List[str],
    optional: List[str],
    label: str,
) -> None:
    print(f"\n  {_BOLD}{label}:{_RESET}")
    all_fields = required + optional
    for field in all_fields:
        value = sample.get(field)
        tag = "[req]" if field in required else "[opt]"
        present = value is not None and str(value).strip() not in ("", "0", "0.00", "0000-00-00", "0000-00-00 00:00:00")
        icon = f"{_GREEN}●{_RESET}" if present else f"{_YELLOW}○{_RESET}"
        display_val = repr(value)[:60] if value is not None else "—"
        print(f"    {icon} {tag:5} {field:<20} {display_val}")

    # Show any extra keys the API returned that we didn't expect
    extra_keys = [k for k in sample if k not in all_fields]
    if extra_keys:
        print(f"\n  {_BLUE}  Extra keys in API response (not mapped):{_RESET}")
        for k in extra_keys:
            print(f"    · {k}: {repr(sample[k])[:60]}")


def _check_data_quality(records: List[Dict], entity: str) -> None:
    """
    Scan all fetched records for known data-quality issues and report them.
    These are warnings, not failures — they document what sanitisation did.
    """
    import re

    html_tag = re.compile(r"<[^>]+>")
    text_fields = {
        "client": ["firstname", "lastname", "companyname", "address1", "city"],
        "invoice": [],
        "transaction": ["description"],
    }.get(entity, [])

    currency_fields = {
        "client": ["currency"],
        "invoice": ["currency", "currencycode"],
        "transaction": ["currency"],
    }.get(entity, [])

    html_hits = 0
    currency_hits = 0

    for r in records:
        for f in text_fields:
            val = str(r.get(f, ""))
            if html_tag.search(val):
                html_hits += 1
                break   # one flag per record is enough

        for f in currency_fields:
            val = r.get(f)
            # Integer 0 or non-alpha strings are bad
            if val is not None and str(val).strip() and not re.match(r"^[A-Za-z]{2,4}$", str(val)):
                currency_hits += 1
                break

    if html_hits:
        _result(
            f"HTML sanitisation",
            WARN,
            f"{html_hits}/{len(records)} {entity} record(s) had HTML in text fields — "
            "stripped before passing to Odoo.",
        )
    if currency_hits:
        _result(
            f"Currency coercion",
            WARN,
            f"{currency_hits}/{len(records)} {entity} record(s) had invalid currency "
            "value (e.g. integer 0) — coerced to '' so Odoo resolver is not called.",
        )


# ============================================================================
# Summary & raw dump
# ============================================================================

def _print_summary(results: Dict[str, bool]) -> None:
    _header("SUMMARY")
    all_passed = all(results.values())
    for check, passed in results.items():
        _result(check, OK if passed else FAIL)

    print()
    if all_passed:
        print(f"  {_GREEN}{_BOLD}All checks passed.{_RESET}")
        print("  The fetcher is ready to be integrated into the Odoo import flow.")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  {_RED}{_BOLD}{len(failed)} check(s) failed:{_RESET} {', '.join(failed)}")
        print("  Review the errors above before proceeding.")
    print()


# ============================================================================
# CLI entry point
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Test WHMCS API connectivity and data shape for the Odoo importer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url", default=os.getenv("WHMCS_URL", ""),
                   help="WHMCS base URL (defaults to WHMCS_URL from .env)")

    auth = p.add_argument_group("Authentication (identifier+secret preferred)")
    auth.add_argument("--id", dest="identifier", default=os.getenv("WHMCS_API_IDENTIFIER", ""),
                    help="API identifier (defaults to WHMCS_API_IDENTIFIER from .env)")
    auth.add_argument("--secret", dest="secret", default=os.getenv("WHMCS_API_SECRET", ""),
                    help="API secret (defaults to WHMCS_API_SECRET from .env)")
    auth.add_argument("--username", default=os.getenv("WHMCS_API_USERNAME", ""),
                    help="Legacy admin username")
    auth.add_argument("--password", default=os.getenv("WHMCS_API_PASSWORD", ""),
                    help="Legacy admin password (plain text)")

    p.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable TLS certificate verification (dev/staging only)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of records to fetch per endpoint during the test (default: 5)",
    )
    p.add_argument(
        "--dump-raw",
        action="store_true",
        help="Print the raw API JSON for the first record of each entity type",
    )
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Validate auth args
    has_api_creds = bool(args.identifier and args.secret)
    has_legacy    = bool(args.username and args.password)
    if not has_api_creds and not has_legacy:
        print(
            f"{_RED}Error:{_RESET} Provide either --id + --secret "
            "(recommended) or --username + --password.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{_BOLD}WHMCS → Odoo Connection Test{_RESET}")
    print(f"  Target : {args.url}")
    auth_mode = "identifier+secret" if has_api_creds else "username+md5(password)"
    print(f"  Auth   : {auth_mode}")
    print(f"  Limit  : {args.limit} record(s) per endpoint")
    if args.no_verify_ssl:
        print(f"  {_YELLOW}TLS verification DISABLED{_RESET}")

    client = WhmcsApiClient(
        base_url=args.url,
        identifier=args.identifier,
        secret=args.secret,
        username=args.username,
        password=args.password,
        verify_ssl=env_bool("WHMCS_VERIFY_SSL", True) and not args.no_verify_ssl,
        timeout=env_timeout(),
    )
    fetcher = WhmcsDataFetcher(client)

    results: Dict[str, bool] = {}

    # 1. Ping
    results["Connectivity & Auth"] = check_ping(client)
    if not results["Connectivity & Auth"]:
        _print_summary(results)
        return 1   # No point continuing if we can't even authenticate

    # 2. Clients
    try:
        clients = fetcher.fetch_clients(limit=args.limit)
        if args.dump_raw and clients:
            _header("RAW CLIENT (first record, post-normalisation)")
            print(json.dumps(clients[0], indent=2, default=str))
    except Exception:
        clients = []

    results["Clients (GetClients)"] = check_clients(fetcher, limit=args.limit)

    # 3. Invoices
    try:
        invoices = fetcher.fetch_invoices(limit=args.limit)
        if args.dump_raw and invoices:
            _header("RAW INVOICE (first record, post-normalisation)")
            print(json.dumps(invoices[0], indent=2, default=str))
    except Exception:
        invoices = []

    results["Invoices (GetInvoices)"] = check_invoices(fetcher, limit=args.limit)

    # 4. Transactions
    try:
        transactions = fetcher.fetch_transactions(limit=args.limit)
        if args.dump_raw and transactions:
            _header("RAW TRANSACTION (first record, post-normalisation)")
            print(json.dumps(transactions[0], indent=2, default=str))
    except Exception:
        transactions = []

    results["Transactions (GetTransactions)"] = check_transactions(fetcher, limit=args.limit)

    _print_summary(results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
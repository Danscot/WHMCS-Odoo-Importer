# -*- coding: utf-8 -*-
"""WHMCS live synchronization bridge."""
import logging
import os
from ..whmcs_connector.env_loader import env_bool, env_timeout, load_dotenv
from ..whmcs_connector.whmcs_api_client import WhmcsApiClient
from ..whmcs_connector.whmcs_data_fetcher import WhmcsDataFetcher

DEFAULT_API_IMPORT_LIMIT = 10
DEFAULT_API_FETCH_CHUNK = 10

_logger = logging.getLogger(__name__)

class LiveSyncError(Exception):
    """Raised when live WHMCS synchronization cannot be prepared."""

def _env_value(name, default=""):
    load_dotenv()
    return os.getenv(name, default).strip()

def build_client():
    """Build an authenticated WHMCS client from server environment/.env."""
    base_url = _env_value("WHMCS_URL")
    identifier = _env_value("WHMCS_API_IDENTIFIER")
    secret = _env_value("WHMCS_API_SECRET")
    username = _env_value("WHMCS_API_USERNAME")
    password = _env_value("WHMCS_API_PASSWORD")
    if not base_url:
        raise LiveSyncError("WHMCS_URL is not configured.")
    if not ((identifier and secret) or (username and password)):
        raise LiveSyncError(
            "WHMCS API credentials are not configured. Set "
            "WHMCS_API_IDENTIFIER + WHMCS_API_SECRET in .env."
        )
    return WhmcsApiClient(
        base_url=base_url,
        identifier=identifier,
        secret=secret,
        username=username,
        password=password,
        verify_ssl=env_bool("WHMCS_VERIFY_SSL", True),
        timeout=env_timeout(),
    )

def _date(value):
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]

def fetch_sync_payload(date_from=None, date_to=None, initial=False):
    """Fetch one stable sync window.

    Initial run: all records.
    Incremental run: clients created, invoices and transactions in the
    inclusive calendar-day window. The lower boundary is deliberately
    repeated because mapping tables make the import idempotent.
    """
    load_dotenv()
    try:
        api_limit = max(1, int(os.getenv("WHMCS_API_IMPORT_LIMIT", str(DEFAULT_API_IMPORT_LIMIT))))
    except (TypeError, ValueError):
        api_limit = DEFAULT_API_IMPORT_LIMIT
    fetcher = WhmcsDataFetcher(build_client())
    if initial:
        # Demo-safe direct fetch path. Background synchronization uses the
        # chunked acquisition path in whmcs.import.batch.
        clients = fetcher.fetch_clients(limit=api_limit)
        invoices = fetcher.fetch_invoices(limit=api_limit)
        transactions = fetcher.fetch_transactions(limit=api_limit)
    else:
        if not date_from or not date_to:
            raise LiveSyncError("Incremental synchronization requires date_from and date_to.")
        start, end = _date(date_from), _date(date_to)
        clients = fetcher.fetch_clients(date_from=start, date_to=end)
        invoices = fetcher.fetch_invoices(date_from=start, date_to=end)
        transactions = fetcher.fetch_transactions(date_from=start, date_to=end)

    payload = {"clients": clients, "invoices": invoices, "transactions": transactions}
    _logger.info(
        "WHMCS live sync fetch: clients=%d invoices=%d transactions=%d window=%s..%s initial=%s",
        len(clients), len(invoices), len(transactions),
        _date(date_from) if date_from else "ALL",
        _date(date_to) if date_to else "NOW", initial,
    )
    return payload

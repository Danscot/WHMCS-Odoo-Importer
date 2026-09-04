# -*- coding: utf-8 -*-
"""
WHMCS API -> importer-compatible data fetcher.

The native WHMCS CSV exports contain more usable fields than the list API
endpoints.  This layer deliberately enriches the list responses so the API
path produces the same *semantic* data expected by WhmcsParser and the Odoo
import engine.

Important WHMCS API differences handled here:
- GetClients is only a summary endpoint; GetClientsDetails is required for
  address, tax id, currency, etc.
- GetInvoices is a header endpoint; GetInvoice is required for invoice items.
- GetTransactions uses ``clientid`` (not ``userid``) as its client filter and
  returns a currency id (often ``0``), not an ISO currency code.
- WHMCS frequently returns a single nested object instead of a list.
- Empty dates such as ``0000-00-00`` must not be sent to Odoo.

The output remains plain dicts using the aliases already understood by
services.whmcs_parser.  No Odoo ORM code belongs in this module.
"""

import html
import logging
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from .whmcs_api_client import WhmcsApiClient, WhmcsApiError

_logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_DEFAULT_ENRICH_WORKERS = 4


class WhmcsDataFetcher:
    """Fetch and enrich WHMCS API data for the existing importer."""

    def __init__(self, client: WhmcsApiClient, page_size: int = _PAGE_SIZE, enrich_workers: Optional[int] = None):
        self.client = client
        self.page_size = max(1, int(page_size or _PAGE_SIZE))
        # Detail endpoints are inherently one-record-per-request in WHMCS.
        # Sequential enrichment was the source of the Odoo 120s request limit
        # on larger datasets. Keep concurrency bounded and configurable.
        configured_workers = enrich_workers if enrich_workers is not None else os.getenv("WHMCS_API_ENRICH_WORKERS", _DEFAULT_ENRICH_WORKERS)
        try:
            self.enrich_workers = max(1, min(16, int(configured_workers)))
        except (TypeError, ValueError):
            self.enrich_workers = _DEFAULT_ENRICH_WORKERS
        self._client_details_cache: Dict[int, dict] = {}
        self._invoice_details_cache: Dict[int, dict] = {}
        self._currency_cache: Dict[int, str] = {}
        self._currencies_loaded = False
        self._client_currency_cache: Dict[int, str] = {}
        self._invoice_currency_cache: Dict[int, str] = {}

    # ------------------------------------------------------------------
    # Incremental API acquisition (one bounded page per cron callback)
    # ------------------------------------------------------------------

    def _fetch_page(self, action, response_key, record_key, offset, chunk_size, extra_params=None):
        params = {"limitstart": max(0, int(offset)), "limitnum": max(1, int(chunk_size))}
        if extra_params:
            params.update(extra_params)
        data = self.client.call(action, **params)
        total = self._to_int(data.get("totalresults"), 0)
        block = data.get(response_key, {})
        raw = block.get(record_key, []) if isinstance(block, dict) else block
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        # WHMCS installations/plugins are not consistent about totalresults:
        # some return the current page size instead of the global total. A full
        # page is therefore treated as potentially having another page. The
        # caller also deduplicates IDs/checkpoints offsets, so this is safe.
        self._last_chunk_has_more = bool(raw) and (len(raw) >= int(chunk_size))
        return raw, total

    def fetch_clients_chunk(self, offset=0, chunk_size=50, date_from=None, date_to=None):
        params = {}
        raw, total = self._fetch_page("GetClients", "clients", "client", offset, chunk_size, params)
        page_count = len(raw)
        if date_from or date_to:
            raw = [r for r in raw if self._date_in_range(r.get("datecreated"), date_from, date_to)]
        details = self._parallel_details(raw, lambda r: self._to_int(r.get("id") or r.get("client_id"), 0), self._get_client_details, label="client details")
        out = []
        for summary in raw:
            cid = self._to_int(summary.get("id") or summary.get("client_id"), 0)
            merged = dict(summary); merged.update(details.get(cid, {}))
            out.append(self._normalise_client(merged))
        return out, page_count

    def fetch_invoices_chunk(self, offset=0, chunk_size=50, date_from=None, date_to=None):
        params = {"orderby": "date", "order": "desc" if date_from else "asc"}
        raw, total = self._fetch_page("GetInvoices", "invoices", "invoice", offset, chunk_size, params)
        page_count = len(raw)
        if date_from or date_to:
            raw = [r for r in raw if self._date_in_range(r.get("date"), date_from, date_to)]
        details = self._parallel_details(raw, lambda r: self._to_int(r.get("id") or r.get("invoiceid"), 0), self._get_invoice_details, label="invoice details")
        out = []
        for header in raw:
            iid = self._to_int(header.get("id") or header.get("invoiceid"), 0)
            merged = dict(header); merged.update(details.get(iid, {}))
            out.append(self._normalise_invoice(merged))
        return out, page_count

    def fetch_transactions_chunk(self, offset=0, chunk_size=50, date_from=None, date_to=None):
        self._ensure_currency_cache()
        raw, total = self._fetch_page("GetTransactions", "transactions", "transaction", offset, chunk_size, {})
        page_count = len(raw)
        if date_from or date_to:
            raw = [r for r in raw if self._date_in_range(r.get("date"), date_from, date_to)]

        # A WHMCS transaction often reports currency=0. The manual export has
        # the effective client/invoice currency, so acquire the client
        # currency before producing canonical transaction records. This is
        # especially important because API acquisition phases use a fresh
        # fetcher/worker and therefore cannot rely on a previous clients phase
        # cache.
        client_ids = {
            self._to_int(r.get("userid") or r.get("client_id"), 0)
            for r in raw
        }
        client_ids.discard(0)
        if client_ids:
            self._parallel_details(
                [{"id": cid} for cid in client_ids],
                lambda r: self._to_int(r.get("id"), 0),
                self._get_client_details,
                label="transaction client currency",
            )

        out = [self._normalise_transaction(r) for r in raw]
        for record in out:
            if not record.get("currency"):
                self._enrich_transaction_currency(record)
        return out, page_count

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def fetch_clients(
        self,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch client summaries and enrich every client with GetClientsDetails."""
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status
        if date_from or date_to:
            params["orderby"] = "datecreated"
            params["sorting"] = "DESC"

        raw_records = self._paginate(
            action="GetClients",
            response_key="clients",
            record_key="client",
            extra_params=params,
            limit=limit,
            stop_if=(
                (lambda r: bool(date_from) and self._date_before(r.get("datecreated"), date_from))
                if date_from else None
            ),
        )

        details = self._parallel_details(
            raw_records,
            lambda r: self._to_int(r.get("id") or r.get("client_id"), 0),
            self._get_client_details,
            label="client details",
        )
        records: List[dict] = []
        for summary in raw_records:
            client_id = self._to_int(summary.get("id") or summary.get("client_id"), 0)
            detail = details.get(client_id, {}) if client_id else {}
            merged = dict(summary)
            # Detail wins for overlapping fields; summary remains a fallback.
            merged.update(detail)
            records.append(self._normalise_client(merged))

        if date_from or date_to:
            records = [
                r for r in records
                if self._date_in_range(r.get("datecreated"), date_from, date_to)
            ]
        return records

    def _parallel_details(self, records, key_fn, fetch_fn, label="details") -> Dict[int, dict]:
        """Fetch one-record WHMCS detail endpoints concurrently, bounded.

        WHMCS has no bulk GetClientsDetails/GetInvoice endpoint. A large
        sequential dataset therefore turns an API sync into hundreds of HTTP
        requests and can exceed Odoo's 120s HTTP limit before the background
        cron is even created. This helper keeps the same data completeness
        while reducing wall-clock time substantially. Failed detail calls are
        logged by their fetch function and simply fall back to the list/header
        record, preserving partial imports.
        """
        ids = []
        seen = set()
        for record in records:
            try:
                ident = key_fn(record)
            except Exception:
                ident = 0
            if ident and ident not in seen:
                seen.add(ident)
                ids.append(ident)

        if not ids:
            return {}
        if self.enrich_workers <= 1 or len(ids) == 1:
            return {ident: (fetch_fn(ident) or {}) for ident in ids}

        result: Dict[int, dict] = {}
        workers = min(self.enrich_workers, len(ids))
        _logger.info("WHMCS: enriching %d %s with %d workers", len(ids), label, workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="whmcs-api") as pool:
            futures = {pool.submit(fetch_fn, ident): ident for ident in ids}
            for future in as_completed(futures):
                ident = futures[future]
                try:
                    result[ident] = future.result() or {}
                except Exception as exc:
                    _logger.warning("WHMCS %s failed for %s: %s", label, ident, exc)
                    result[ident] = {}
        return result

    def _get_client_details(self, client_id: int) -> dict:
        """Get and cache the full WHMCS client record."""
        if client_id <= 0:
            return {}
        if client_id in self._client_details_cache:
            return self._client_details_cache[client_id]

        try:
            data = self.client.call("GetClientsDetails", clientid=client_id, stats=False)
        except WhmcsApiError as exc:
            # Do not destroy a complete import because one malformed/closed
            # client cannot be expanded.  The summary is still useful.
            _logger.warning(
                "WHMCS GetClientsDetails failed for client %s; using summary data: %s",
                client_id, exc,
            )
            self._client_details_cache[client_id] = {}
            return {}

        detail = data.get("client", {})
        if not isinstance(detail, dict):
            detail = {}
        self._client_details_cache[client_id] = detail

        currency = self._currency_code_from_values(
            detail.get("currency_code"), detail.get("currency")
        )
        if currency:
            self._client_currency_cache[client_id] = currency
        return detail

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    def fetch_invoices(
        self,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        userid: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch invoice headers and enrich them with GetInvoice line items."""
        params: Dict[str, Any] = {"orderby": "date", "order": "desc" if date_from else "asc"}
        if status:
            params["status"] = status
        if userid is not None:
            params["userid"] = userid

        raw_records = self._paginate(
            action="GetInvoices",
            response_key="invoices",
            record_key="invoice",
            extra_params=params,
            limit=limit,
            stop_if=(
                (lambda r: bool(date_from) and self._date_before(r.get("date"), date_from))
                if date_from else None
            ),
        )

        details = self._parallel_details(
            raw_records,
            lambda r: self._to_int(r.get("id") or r.get("invoiceid"), 0),
            self._get_invoice_details,
            label="invoice details",
        )
        records: List[dict] = []
        for header in raw_records:
            invoice_id = self._to_int(header.get("id") or header.get("invoiceid"), 0)
            detail = details.get(invoice_id, {}) if invoice_id else {}
            merged = dict(header)
            # GetInvoice has the authoritative invoice totals/status/dates/items.
            # Header fields remain as fallback for installations that restrict it.
            merged.update(detail)
            records.append(self._normalise_invoice(merged))

        if date_from or date_to:
            records = [
                r for r in records
                if self._date_in_range(r.get("date"), date_from, date_to)
            ]
        return records

    def fetch_invoice_lines(self, invoice_id: int) -> List[dict]:
        """Public helper returning normalized line dictionaries for one invoice."""
        detail = self._get_invoice_details(self._to_int(invoice_id, 0))
        return self._normalise_invoice_items(detail.get("items"))

    def _get_invoice_details(self, invoice_id: int) -> dict:
        """Get and cache a full invoice, including items and transactions."""
        if invoice_id <= 0:
            return {}
        if invoice_id in self._invoice_details_cache:
            return self._invoice_details_cache[invoice_id]

        try:
            data = self.client.call("GetInvoice", invoiceid=invoice_id)
        except WhmcsApiError as exc:
            # GetInvoices itself is still usable; only line-level enrichment
            # is lost for this invoice.
            _logger.warning(
                "WHMCS GetInvoice failed for invoice %s; using header data: %s",
                invoice_id, exc,
            )
            self._invoice_details_cache[invoice_id] = {}
            return {}

        detail = dict(data)
        # Some WHMCS versions return the useful object under invoice.
        if isinstance(data.get("invoice"), dict):
            detail = dict(data["invoice"])
            for key, value in data.items():
                if key not in detail:
                    detail[key] = value
        self._invoice_details_cache[invoice_id] = detail

        currency = self._currency_code_from_values(
            detail.get("currency_code"), detail.get("currencycode"), detail.get("currency")
        )
        if currency:
            self._invoice_currency_cache[invoice_id] = currency
        return detail

    # ------------------------------------------------------------------
    # Transactions / payments
    # ------------------------------------------------------------------

    def fetch_transactions(
        self,
        limit: Optional[int] = None,
        userid: Optional[int] = None,
        invoiceid: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch WHMCS transactions used as Odoo payments.

        IMPORTANT: WHMCS documents ``clientid`` for GetTransactions.  The old
        implementation sent ``userid``, which is not a documented filter.
        Date filtering is performed locally because datefrom/dateto are not
        documented GetTransactions parameters.
        """
        params: Dict[str, Any] = {}
        if userid is not None:
            params["clientid"] = userid
        if invoiceid is not None:
            params["invoiceid"] = invoiceid

        raw_records = self._paginate(
            action="GetTransactions",
            response_key="transactions",
            record_key="transaction",
            extra_params=params,
            limit=None if (date_from or date_to) else limit,
        )

        # Currency IDs are cheap to resolve once and then reused for every
        # transaction. A standalone transaction fetch may not have a client
        # phase, so seed client currencies before canonicalizing records.
        self._ensure_currency_cache()
        client_ids = {
            self._to_int(r.get("userid") or r.get("client_id"), 0)
            for r in raw_records
        }
        client_ids.discard(0)
        if client_ids:
            self._parallel_details(
                [{"id": cid} for cid in client_ids],
                lambda r: self._to_int(r.get("id"), 0),
                self._get_client_details,
                label="transaction client currency",
            )
        records = [self._normalise_transaction(r) for r in raw_records]

        # Resolve transaction currency after invoices/clients where possible.
        # Avoid another invoice-detail request when the transaction already has
        # a gateway/payment method; the transaction itself is authoritative.
        for record in records:
            if not record.get("currency"):
                self._enrich_transaction_currency(record)

        if date_from or date_to:
            records = [
                r for r in records
                if self._date_in_range(r.get("date"), date_from, date_to)
            ]
            if limit is not None:
                records = records[:limit]
        return records

    def _enrich_transaction_currency(self, record: dict) -> None:
        """Resolve WHMCS transaction currency id/zero value to an ISO code."""
        txn_id = self._to_int(record.get("id"), 0)
        client_id = self._to_int(record.get("userid") or record.get("client_id"), 0)
        invoice_id = self._to_int(record.get("invoiceid") or record.get("invoice_id"), 0)

        raw_currency = record.get("_raw_currency")
        code = self._currency_code_from_values(raw_currency)
        # Do not call GetInvoice while acquiring transactions. A transaction
        # page can contain many invoices and each GetInvoice is a secondary
        # network request that can block an Odoo cron worker. Currency is a
        # convenience field; if it is not already available, leave it blank
        # and let the normal import fallback handle it.
        if not code and invoice_id:
            code = self._invoice_currency_cache.get(invoice_id, "")
        if not code and client_id:
            code = self._client_currency_cache.get(client_id, "")

        if not code and raw_currency not in (None, "", 0, "0"):
            code = self._currency_code_from_values(raw_currency)
        if code:
            record["currency"] = code
            return

        if raw_currency in (0, "0", None, ""):
            _logger.debug(
                "WHMCS transaction %s has no explicit currency code; leaving currency blank",
                txn_id,
            )

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(value) -> str:
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = re.sub(r"<[^>]*>", "", text)
        return text.strip()

    @staticmethod
    def _safe_currency(value) -> str:
        if value is None or value == "":
            return ""
        text = str(value).strip().upper()
        return text if re.fullmatch(r"[A-Z]{3}", text) else ""

    def _currency_code_from_values(self, *values) -> str:
        """Accept an ISO code or a WHMCS currency id and return ISO code."""
        for value in values:
            code = self._safe_currency(value)
            if code:
                return code

            if value not in (None, "", False):
                try:
                    currency_id = int(str(value).strip())
                except (TypeError, ValueError):
                    continue
                if currency_id > 0:
                    self._ensure_currency_cache()
                    code = self._currency_cache.get(currency_id, "")
                    if code:
                        return code
        return ""

    def _ensure_currency_cache(self) -> None:
        if self._currencies_loaded:
            return
        self._currencies_loaded = True
        try:
            data = self.client.call("GetCurrencies")
        except WhmcsApiError as exc:
            _logger.warning("Could not load WHMCS currencies: %s", exc)
            return

        raw = data.get("currencies", {})
        if isinstance(raw, dict):
            raw = raw.get("currency", [])
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []

        for item in raw:
            if not isinstance(item, dict):
                continue
            currency_id = self._to_int(item.get("id"), 0)
            code = self._safe_currency(item.get("code"))
            if currency_id > 0 and code:
                self._currency_cache[currency_id] = code

    @classmethod
    def _normalise_client(cls, raw: dict) -> dict:
        strip = cls._strip_html
        currency = cls._currency_code_from_raw_static(raw)
        microsoft_id = cls._microsoft_id_from_customfields(raw.get("customfields"))
        return {
            "id": raw.get("id") or raw.get("client_id") or raw.get("userid"),
            "firstname": strip(raw.get("firstname", "")),
            "lastname": strip(raw.get("lastname", "")),
            "companyname": strip(raw.get("companyname", "")),
            "email": str(raw.get("email") or "").strip(),
            "phonenumber": str(raw.get("phonenumber") or raw.get("phone") or raw.get("telephoneNumber") or "").strip(),
            "phone": str(raw.get("phone") or raw.get("phonenumber") or raw.get("telephoneNumber") or "").strip(),
            "mobile": str(raw.get("mobile") or "").strip(),
            "address1": strip(raw.get("address1", "")),
            "address2": strip(raw.get("address2", "")),
            "city": strip(raw.get("city", "")),
            "state": strip(raw.get("state", "") or raw.get("fullstate", "")),
            "postcode": strip(raw.get("postcode", "")),
            "country": str(raw.get("country") or raw.get("countrycode") or "").strip().upper(),
            "language": str(raw.get("language") or "").strip(),
            "datecreated": raw.get("datecreated") or raw.get("created_at") or "",
            "status": raw.get("status") or "",
            "tax_id": strip(raw.get("tax_id") or raw.get("taxid") or raw.get("vat") or ""),
            "microsoft_id": str(
                raw.get("microsoft_id")
                or raw.get("Microsoft ID")
                or microsoft_id
                or ""
            ).strip(),
            "customfields": raw.get("customfields") or [],
            "currency": currency,
        }

    @staticmethod
    def _microsoft_id_from_customfields(customfields) -> str:
        """Extract Microsoft ID from named WHMCS custom fields.

        Some WHMCS installations return only ``id`` + ``value``. In that
        case, WHMCS_MICROSOFT_CUSTOM_FIELD_ID can identify the field without
        hard-coding a tenant-specific numeric ID.
        """
        if not isinstance(customfields, (list, tuple)):
            return ""
        configured_id = os.environ.get("WHMCS_MICROSOFT_CUSTOM_FIELD_ID", "").strip()
        for field in customfields:
            if not isinstance(field, dict):
                continue
            label = " ".join(
                str(field.get(k) or "").strip().lower()
                for k in ("name", "fieldname", "displayname", "description")
            )
            field_id = str(field.get("id") or "").strip()
            if "microsoft" in label or (configured_id and field_id == configured_id):
                return str(field.get("value") or "").strip()
        return ""

    @staticmethod
    def _currency_code_from_raw_static(raw: dict) -> str:
        for value in (raw.get("currency_code"), raw.get("currencycode"), raw.get("currency")):
            if value is None or value == "":
                continue
            text = str(value).strip().upper()
            if re.fullmatch(r"[A-Z]{3}", text):
                return text
        return ""

    def _normalise_invoice(self, raw: dict) -> dict:
        currency = self._currency_code_from_values(
            raw.get("currency_code"), raw.get("currencycode"), raw.get("currency")
        )
        invoice_id = self._to_int(raw.get("id") or raw.get("invoiceid"), 0)
        if invoice_id and currency:
            self._invoice_currency_cache[invoice_id] = currency

        items = raw.get("items") if raw.get("items") is not None else raw.get("lines")
        lines = self._normalise_invoice_items(items)
        return {
            "id": invoice_id,
            "userid": raw.get("userid") or raw.get("client_id") or raw.get("user_id"),
            "client_id": raw.get("userid") or raw.get("client_id") or raw.get("user_id"),
            "invoicenum": raw.get("invoicenum", ""),
            "invoice_number": raw.get("invoicenum") or raw.get("invoice_number") or "",
            "date": self._clean_api_date(raw.get("date") or raw.get("invoice_date")),
            "duedate": self._clean_api_date(raw.get("duedate") or raw.get("due_date")),
            "due_date": self._clean_api_date(raw.get("duedate") or raw.get("due_date")),
            "datepaid": self._clean_api_date(raw.get("datepaid")),
            "subtotal": raw.get("subtotal", "0.00"),
            "credit": raw.get("credit", "0.00"),
            "tax": raw.get("tax", "0.00"),
            "tax2": raw.get("tax2", "0.00"),
            "total": raw.get("total", "0.00"),
            "status": raw.get("status", ""),
            "paymentmethod": raw.get("paymentmethod", ""),
            "notes": self._strip_html(raw.get("notes", "")),
            "currency": currency,
            "currencycode": currency,
            "lines": lines,
        }

    def _normalise_invoice_items(self, items) -> List[dict]:
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return []

        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            amount = item.get("amount")
            # WHMCS GetInvoice item amounts are line totals. The importer
            # expects a unit price; native WHMCS items normally have quantity 1.
            quantity = item.get("quantity") or item.get("qty") or 1
            lines.append({
                "description": self._strip_html(item.get("description") or item.get("item") or ""),
                "quantity": quantity,
                "unit_price": amount if amount is not None else item.get("unit_price", 0),
                "product_id": item.get("relid") or item.get("product_id"),
            })
        return lines

    def _normalise_transaction(self, raw: dict) -> dict:
        raw_currency = raw.get("currency")
        client_id = raw.get("userid") or raw.get("client_id")
        invoice_id = raw.get("invoiceid") or raw.get("invoice_id")
        gateway = str(raw.get("gateway") or raw.get("paymentmethod") or "").strip()
        return {
            "id": raw.get("id"),
            "invoiceid": invoice_id,
            "invoice_id": invoice_id,
            "userid": client_id,
            "client_id": client_id,
            "transid": str(raw.get("transid") or raw.get("transaction_id") or "").strip(),
            "transaction_id": str(raw.get("transid") or raw.get("transaction_id") or "").strip(),
            "date": self._clean_api_date(raw.get("date")),
            "amountin": raw.get("amountin", raw.get("amount", "0.00")),
            "amountout": raw.get("amountout", "0.00"),
            "fees": raw.get("fees", "0.00"),
            "gateway": gateway,
            "paymentmethod": str(raw.get("paymentmethod") or gateway).strip(),
            "currency": self._currency_code_from_values(raw_currency),
            "description": self._strip_html(raw.get("description", "")),
            "_raw_currency": raw_currency,
        }

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_api_date(value):
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.startswith("0000-00-00"):
            return None
        return text

    @classmethod
    def _date_before(cls, value, boundary) -> bool:
        value = cls._clean_api_date(value)
        return bool(value and boundary and value[:10] < str(boundary)[:10])

    @classmethod
    def _date_in_range(cls, value, date_from=None, date_to=None) -> bool:
        value = cls._clean_api_date(value)
        if not value:
            return False
        day = value[:10]
        if date_from and day < str(date_from)[:10]:
            return False
        if date_to and day > str(date_to)[:10]:
            return False
        return True

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _paginate(
        self,
        action: str,
        response_key: str,
        record_key: str,
        extra_params: dict,
        limit: Optional[int],
        stop_if=None,
    ) -> List[dict]:
        records: List[dict] = []
        offset = 0
        effective_page = min(self.page_size, limit) if limit else self.page_size

        while True:
            params = {
                "limitstart": offset,
                "limitnum": effective_page,
                **extra_params,
            }
            data = self.client.call(action, **params)
            total = self._to_int(data.get("totalresults"), 0)
            page_data = data.get(response_key, {})

            if isinstance(page_data, dict):
                raw_list = page_data.get(record_key, [])
            elif isinstance(page_data, list):
                raw_list = page_data
            else:
                raw_list = []
            if isinstance(raw_list, dict):
                raw_list = [raw_list]

            if stop_if and any(stop_if(record) for record in raw_list):
                records.extend(raw_list)
                return records[:limit] if limit else records

            records.extend(raw_list)
            fetched = len(records)
            _logger.debug("WHMCS %s: fetched %d/%d (offset=%d)", action, fetched, total, offset)

            if limit and fetched >= limit:
                return records[:limit]
            if not raw_list or (total and fetched >= total):
                return records

            offset += len(raw_list)
            if limit:
                effective_page = min(self.page_size, max(1, limit - fetched))

    @staticmethod
    def _to_int(value, default=0) -> int:
        try:
            if value in (None, ""):
                return default
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default

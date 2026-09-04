# -*- coding: utf-8 -*-
"""
WHMCS Export Parser.

Converts raw WHMCS JSON or CSV exports into NormalizedExport objects.

The WHMCS web export format used by this module is section based.  A CSV
export normally starts with a title row such as ``Clients``, ``Invoices`` or
``Transactions`` followed by the real CSV header.  This parser detects and
skips that title row and maps the native WHMCS column names to the internal
normalizer structures.

Supported CSV exports:
    - Clients
    - Invoices
    - Transactions

JSON exports keep the original combined format (clients/invoices/transactions).
"""
import csv
import io
import json
import logging
from typing import Optional

from .normalizer import (
    NormalizedClient,
    NormalizedExport,
    NormalizedInvoice,
    NormalizedInvoiceLine,
    NormalizedTransaction,
)

_logger = logging.getLogger(__name__)


class WhmcsParseError(Exception):
    """Raised when the export file cannot be parsed."""


class WhmcsParser:
    """Parse WHMCS JSON or section-based CSV exports."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_bytes(self, content: bytes, filename: str = "") -> NormalizedExport:
        """Detect format from filename/content and delegate."""
        fname = (filename or "").lower()
        if fname.endswith(".csv"):
            return self._parse_csv(content)
        return self._parse_json(content)

    def parse_api_payload(self, payload) -> NormalizedExport:
        """
        Parse an already-acquired WHMCS API payload through the same record
        parsers used by manual JSON/CSV imports.

        API acquisition may enrich/fetch records, but it must not define a
        second internal data model. The parser is the single truth boundary
        before matching and importing.
        """
        if not isinstance(payload, dict):
            raise WhmcsParseError("Expected the WHMCS API payload to be an object.")

        def records(key):
            value = payload.get(key, [])
            if value is None:
                return []
            if not isinstance(value, list):
                raise WhmcsParseError(
                    "WHMCS API payload field '%s' must be a list." % key
                )
            return value

        clients = [self._parse_client(c) for c in records("clients")]
        invoices = [self._parse_invoice(i) for i in records("invoices")]
        transactions = [self._parse_transaction(t) for t in records("transactions")]

        invalid = (
            sum(c.whmcs_id <= 0 for c in clients),
            sum(i.whmcs_invoice_id <= 0 for i in invoices),
            sum(t.whmcs_transaction_id <= 0 for t in transactions),
        )
        if any(invalid):
            raise WhmcsParseError(
                "WHMCS API payload contains records without valid IDs "
                "(clients=%d, invoices=%d, transactions=%d)." % invalid
            )

        export = NormalizedExport(
            clients=clients, invoices=invoices, transactions=transactions
        )
        _logger.info(
            "WHMCS Parser (API canonical): parsed %d clients, %d invoices, %d transactions",
            len(clients), len(invoices), len(transactions),
        )
        return export

    def parse_uploaded_csvs(
        self,
        clients_content: Optional[bytes] = None,
        clients_filename: str = "",
        invoices_content: Optional[bytes] = None,
        invoices_filename: str = "",
        transactions_content: Optional[bytes] = None,
        transactions_filename: str = "",
    ) -> NormalizedExport:
        """
        Parse any combination of the three WHMCS CSV exports and merge them.

        The files are independent in the WHMCS export format, but their IDs
        reference each other:
            invoice.User ID   -> client.ID
            transaction.User ID -> client.ID
            transaction.Invoice ID -> invoice.ID
        """
        exports = []

        for content, filename in (
            (clients_content, clients_filename),
            (invoices_content, invoices_filename),
            (transactions_content, transactions_filename),
        ):
            if content:
                parsed = self._parse_csv(content)
                exports.append(parsed)

        if not exports:
            raise WhmcsParseError("No CSV export files were provided.")

        merged = NormalizedExport()
        for parsed in exports:
            merged.clients.extend(parsed.clients)
            merged.invoices.extend(parsed.invoices)
            merged.transactions.extend(parsed.transactions)

        # Protect against accidental duplicate rows when the same file is
        # uploaded twice.
        merged.clients = self._dedupe_by_id(merged.clients, "whmcs_id")
        merged.invoices = self._dedupe_by_id(merged.invoices, "whmcs_invoice_id")
        merged.transactions = self._dedupe_by_id(
            merged.transactions, "whmcs_transaction_id"
        )

        _logger.info(
            "WHMCS Parser (CSV bundle): parsed %d clients, %d invoices, %d transactions",
            len(merged.clients),
            len(merged.invoices),
            len(merged.transactions),
        )
        return merged

    # ------------------------------------------------------------------
    # JSON parser
    # ------------------------------------------------------------------

    def _parse_json(self, content: bytes) -> NormalizedExport:
        try:
            raw = json.loads(content.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WhmcsParseError(f"Cannot decode JSON export: {exc}") from exc

        if not isinstance(raw, dict):
            raise WhmcsParseError("Expected a JSON object at the top level.")

        return self.parse_api_payload(raw)

    def _parse_client(self, raw: dict) -> NormalizedClient:
        microsoft_id = (
            raw.get("microsoft_id")
            or raw.get("Microsoft ID")
            or raw.get("microsoftid")
            or self._extract_microsoft_id(raw.get("customfields"))
            or ""
        )
        return NormalizedClient(
            whmcs_id=self._to_int(
                raw.get("id") or raw.get("ID") or raw.get("client_id"),
                default=0,
            ),
            first_name=raw.get("firstname") or raw.get("First Name") or raw.get("first_name") or "",
            last_name=raw.get("lastname") or raw.get("Last Name") or raw.get("last_name") or "",
            company_name=(
                raw.get("companyname")
                or raw.get("Company Name")
                or raw.get("company_name")
                or raw.get("company")
                or ""
            ),
            email=raw.get("email") or raw.get("Email") or "",
            phone=raw.get("phone") or raw.get("Phone Number") or raw.get("phonenumber") or "",
            mobile=raw.get("mobile") or raw.get("Mobile") or "",
            address1=raw.get("address1") or raw.get("Address 1") or raw.get("street") or "",
            address2=raw.get("address2") or raw.get("Address 2") or raw.get("street2") or "",
            city=raw.get("city") or raw.get("City") or "",
            state=raw.get("state") or raw.get("State") or "",
            postcode=raw.get("postcode") or raw.get("Postcode") or raw.get("zip") or "",
            country=raw.get("country") or raw.get("Country") or "",
            tax_id=raw.get("tax_id") or raw.get("taxid") or raw.get("Tax ID") or raw.get("vat") or "",
            language=raw.get("language") or raw.get("Language") or raw.get("lang") or "",
            microsoft_id=microsoft_id,
            currency=raw.get("currency") or raw.get("Currency") or raw.get("currency_code") or "",
        )

    @staticmethod
    def _extract_microsoft_id(customfields) -> str:
        if not isinstance(customfields, (list, tuple)):
            return ""
        for field in customfields:
            if not isinstance(field, dict):
                continue
            label = " ".join(
                str(field.get(k) or "").strip().lower()
                for k in ("name", "fieldname", "displayname", "description")
            )
            if "microsoft" in label:
                return str(field.get("value") or "").strip()
        return ""

    def _parse_invoice(self, raw: dict) -> NormalizedInvoice:
        lines_raw = raw.get("lines") or raw.get("items") or []
        lines = [self._parse_invoice_line(line) for line in lines_raw]
        return NormalizedInvoice(
            whmcs_invoice_id=self._to_int(
                raw.get("id") or raw.get("ID") or raw.get("invoice_id"),
                default=0,
            ),
            whmcs_client_id=self._to_int(
                raw.get("client_id")
                or raw.get("userid")
                or raw.get("User ID")
                or raw.get("user_id"),
                default=0,
            ),
            invoice_number=str(
                raw.get("invoice_number")
                or raw.get("Invoice Number")
                or raw.get("invoicenum")
                or ""
            ),
            date=self._clean_date(
                raw.get("date")
                or raw.get("invoice_date")
                or raw.get("Creation Date")
                or raw.get("created_at")
            ),
            due_date=self._clean_date(
                raw.get("due_date")
                or raw.get("duedate")
                or raw.get("Due Date")
            ),
            currency=raw.get("currency") or raw.get("Currency") or raw.get("currency_code") or "",
            status=raw.get("status") or raw.get("Status") or "",
            total=self._to_float(
                raw.get("total") or raw.get("Total"),
                default=0.0,
            ),
            lines=lines,
        )

    def _parse_invoice_line(self, raw: dict) -> NormalizedInvoiceLine:
        return NormalizedInvoiceLine(
            description=raw.get("description") or raw.get("item") or "",
            quantity=self._to_float(
                raw.get("quantity") or raw.get("qty"),
                default=1.0,
            ),
            unit_price=self._to_float(
                raw.get("unit_price") or raw.get("amount"),
                default=0.0,
            ),
            whmcs_product_id=self._to_int(
                raw.get("product_id") or raw.get("relid"),
                default=None,
            ),
        )

    def _parse_transaction(self, raw: dict) -> NormalizedTransaction:
        amount_in = self._to_float(
            raw.get("amount")
            or raw.get("amountin")
            or raw.get("Amount In"),
            default=0.0,
        )
        amount_out = self._to_float(
            raw.get("amountout")
            or raw.get("Amount Out"),
            default=0.0,
        )
        return NormalizedTransaction(
            whmcs_transaction_id=self._to_int(
                raw.get("id") or raw.get("ID") or raw.get("transaction_id"),
                default=0,
            ),
            transaction_reference=str(
                raw.get("transaction_id")
                or raw.get("Transaction ID")
                or raw.get("transid")
                or ""
            ),
            whmcs_invoice_id=self._to_int(
                raw.get("invoice_id")
                or raw.get("invoiceid")
                or raw.get("Invoice ID"),
                default=None,
            ),
            whmcs_client_id=self._to_int(
                raw.get("client_id")
                or raw.get("userid")
                or raw.get("User ID"),
                default=None,
            ),
            date=self._clean_date(
                raw.get("date")
                or raw.get("created_at")
                or raw.get("Date")
            ),
            amount=max(amount_in - amount_out, 0.0),
            amount_in=amount_in,
            amount_out=amount_out,
            currency=raw.get("currency") or raw.get("Currency") or raw.get("currency_code") or "",
            gateway=raw.get("gateway") or raw.get("paymentmethod") or raw.get("Payment Method") or "",
            description=raw.get("description") or raw.get("Description") or "",
        )

    # ------------------------------------------------------------------
    # CSV parser
    # ------------------------------------------------------------------

    def _parse_csv(self, content: bytes) -> NormalizedExport:
        """
        Parse a WHMCS CSV export.

        WHMCS places a section title on the first row:
            Clients
            ID,First Name,Last Name,...

        The old importer treated "Clients" as the CSV header.  This method
        explicitly finds the real header and detects the section from it.
        """
        rows = self._read_csv_rows(content)
        if not rows:
            raise WhmcsParseError("The CSV file is empty.")

        header_index, header = self._find_csv_header(rows)
        normalized_header = {self._header_key(value) for value in header}

        data_rows = rows[header_index + 1 :]
        reader = csv.DictReader(
            io.StringIO("\n".join(self._csv_row_to_line(row) for row in data_rows)),
            fieldnames=header,
        )

        # Detect section from the actual WHMCS column names.
        if "first name" in normalized_header or "company name" in normalized_header:
            section = "clients"
        elif "invoice number" in normalized_header or "subtotal" in normalized_header:
            section = "invoices"
        elif "amount in" in normalized_header or "payment method" in normalized_header:
            section = "transactions"
        else:
            raise WhmcsParseError(
                "Unrecognized WHMCS CSV structure. Expected a Clients, Invoices, "
                "or Transactions export."
            )

        parsed = NormalizedExport()

        for row in reader:
            # Ignore completely blank rows.
            if not any(str(value or "").strip() for value in row.values()):
                continue

            try:
                if section == "clients":
                    record = self._parse_client(row)
                    if record.whmcs_id <= 0:
                        _logger.warning("Skipping client CSV row without a valid ID: %s", row)
                        continue
                    parsed.clients.append(record)

                elif section == "invoices":
                    record = self._parse_invoice(row)
                    if record.whmcs_invoice_id <= 0:
                        _logger.warning("Skipping invoice CSV row without a valid ID: %s", row)
                        continue
                    parsed.invoices.append(record)

                else:
                    record = self._parse_transaction(row)
                    if record.whmcs_transaction_id <= 0:
                        _logger.warning(
                            "Skipping transaction CSV row without a valid ID: %s", row
                        )
                        continue
                    parsed.transactions.append(record)

            except Exception as exc:
                _logger.warning(
                    "WHMCS %s CSV row parse error: %s — %s",
                    section,
                    row,
                    exc,
                )

        _logger.info(
            "WHMCS Parser (CSV %s): parsed %d clients, %d invoices, %d transactions",
            section,
            len(parsed.clients),
            len(parsed.invoices),
            len(parsed.transactions),
        )
        return parsed

    @staticmethod
    def _read_csv_rows(content: bytes) -> list:
        text = content.decode("utf-8-sig", errors="replace")
        # csv.reader handles quoted commas correctly.
        return list(csv.reader(io.StringIO(text)))

    @staticmethod
    def _find_csv_header(rows: list):
        """Find the actual header row, ignoring title/preamble rows."""
        header_candidates = (
            {"id", "first name", "last name", "company name", "email"},
            {"id", "user id", "client name", "invoice number", "total"},
            {"id", "user id", "client name", "amount in", "amount out"},
        )

        for index, row in enumerate(rows):
            normalized = {WhmcsParser._header_key(v) for v in row if v is not None}
            for candidate in header_candidates:
                if candidate.issubset(normalized):
                    return index, row

        raise WhmcsParseError(
            "Could not find a WHMCS CSV header row. The file may not be a "
            "Clients, Invoices, or Transactions export."
        )

    @staticmethod
    def _header_key(value) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _csv_row_to_line(row: list) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="")
        writer.writerow(row)
        return output.getvalue()

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_int(value, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_float(value, default=0.0):
        if value is None or str(value).strip() == "":
            return default
        try:
            return float(str(value).strip().replace(",", ""))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _clean_date(value):
        if value is None:
            return None
        value = str(value).strip()
        if not value or value.startswith("0000-00-00"):
            return None
        # Odoo accepts the YYYY-MM-DD part for both Date and Datetime fields.
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
        return value

    @staticmethod
    def _dedupe_by_id(records, attr):
        seen = set()
        result = []
        for record in records:
            key = getattr(record, attr, None)
            if key in seen:
                continue
            seen.add(key)
            result.append(record)
        return result

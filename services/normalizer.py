# -*- coding: utf-8 -*-
"""
Normalized internal representations of WHMCS data.

These dataclasses decouple raw WHMCS JSON/CSV keys from the Odoo import engine.
If the WHMCS export format changes, only the parser needs updating — not the engine.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Phone normalization helper
# ---------------------------------------------------------------------------

def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Normalize a phone number to digits only for comparison purposes.
    Examples:
        +237 6 00 00 00 01  →  237600000001
        237600000001        →  237600000001
        600000001           →  600000001
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if digits else None


def normalize_email(raw: Optional[str]) -> Optional[str]:
    """Lowercase and strip an email address."""
    if not raw:
        return None
    return raw.strip().lower()


def normalize_vat(raw: Optional[str]) -> Optional[str]:
    """Strip whitespace and uppercase a VAT/tax ID."""
    if not raw:
        return None
    return raw.strip().upper()


# ---------------------------------------------------------------------------
# Normalized data structures
# ---------------------------------------------------------------------------

@dataclass
class NormalizedClient:
    whmcs_id: int
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    email: str = ""
    phone: str = ""
    mobile: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    postcode: str = ""
    country: str = ""
    tax_id: str = ""
    language: str = ""
    microsoft_id: str = ""
    currency: str = ""

    @property
    def display_name(self) -> str:
        if self.company_name:
            return self.company_name
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p).strip() or f"WHMCS Client #{self.whmcs_id}"

    @property
    def normalized_email(self) -> Optional[str]:
        return normalize_email(self.email)

    @property
    def normalized_phone(self) -> Optional[str]:
        return normalize_phone(self.phone)

    @property
    def normalized_mobile(self) -> Optional[str]:
        return normalize_phone(self.mobile)

    @property
    def normalized_vat(self) -> Optional[str]:
        return normalize_vat(self.tax_id)


@dataclass
class NormalizedInvoiceLine:
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    whmcs_product_id: Optional[int] = None

    @property
    def subtotal(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class NormalizedInvoice:
    whmcs_invoice_id: int
    whmcs_client_id: int
    invoice_number: str = ""
    date: Optional[str] = None          # ISO 8601 string YYYY-MM-DD
    due_date: Optional[str] = None
    currency: str = ""
    status: str = ""                    # Paid, Unpaid, Cancelled, Refunded, …
    total: float = 0.0
    lines: List[NormalizedInvoiceLine] = field(default_factory=list)

    @property
    def is_paid(self) -> bool:
        return self.status.lower() == "paid"

    @property
    def is_cancelled(self) -> bool:
        return self.status.lower() in ("cancelled", "canceled")

    @property
    def is_refunded(self) -> bool:
        return self.status.lower() == "refunded"


@dataclass
class NormalizedTransaction:
    whmcs_transaction_id: int
    transaction_reference: str = ""
    whmcs_invoice_id: Optional[int] = None
    whmcs_client_id: Optional[int] = None
    date: Optional[str] = None
    amount: float = 0.0
    # WHMCS transaction CSVs expose separate inbound/outbound columns.
    # Keep both values so the importer can refuse outbound/zero transactions
    # instead of accidentally creating an inbound Odoo payment for them.
    amount_in: float = 0.0
    amount_out: float = 0.0
    currency: str = ""
    gateway: str = ""
    description: str = ""

    @property
    def is_inbound(self) -> bool:
        return self.amount_in > 0.0 and self.amount > 0.0

    @property
    def fingerprint(self) -> str:
        """Stable dedup key combining multiple fields."""
        parts = [
            str(self.whmcs_client_id or ""),
            str(self.whmcs_invoice_id or ""),
            str(self.amount),
            str(self.date or ""),
            self.gateway,
            self.transaction_reference,
        ]
        return "|".join(parts)


@dataclass
class NormalizedExport:
    clients: List[NormalizedClient] = field(default_factory=list)
    invoices: List[NormalizedInvoice] = field(default_factory=list)
    transactions: List[NormalizedTransaction] = field(default_factory=list)

    @property
    def client_map(self):
        return {c.whmcs_id: c for c in self.clients}

    @property
    def invoice_map(self):
        return {i.whmcs_invoice_id: i for i in self.invoices}

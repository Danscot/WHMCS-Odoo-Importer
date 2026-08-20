# -*- coding: utf-8 -*-
"""
Payment Importer.

Creates Odoo account.payment records from NormalizedTransaction objects.

WHMCS transactions represent inbound payments (money received from customers).
payment_type = "inbound"

Where possible, the payment is linked to the Odoo invoice so reconciliation
can occur automatically through Odoo's standard mechanisms.
"""
import logging
from datetime import date

from odoo import fields

from .normalizer import NormalizedTransaction

_logger = logging.getLogger(__name__)


class PaymentImporter:

    def __init__(self, env, config):
        self.env = env
        self.config = config
        self._currency_cache = {}
        self._journal_cache = {}

    def create_payment(self, txn: NormalizedTransaction, partner_id: int, invoice_id: int = None):
        """Create, post and optionally reconcile one inbound payment."""
        payment = self.env["account.payment"].create(
            self.build_payment_vals(txn, partner_id)
        )
        payment.action_post()
        if invoice_id:
            self._reconcile_with_invoice(payment, invoice_id)
        return payment

    def build_payment_vals(self, txn: NormalizedTransaction, partner_id: int) -> dict:
        """Build an Odoo account.payment payload without performing a create."""
        journal = self._resolve_journal(txn.gateway)
        if not journal:
            raise ValueError(
                f"No bank/cash journal is configured for WHMCS gateway '{txn.gateway or 'default'}'."
            )
        payment_method_line = self._resolve_payment_method_line(journal)
        if not payment_method_line:
            raise ValueError(f"Journal '{journal.display_name}' has no inbound payment method line.")

        vals = {
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": int(partner_id),
            "amount": float(txn.amount),
            "date": txn.date or date.today().isoformat(),
            "journal_id": journal.id,
            "payment_method_line_id": payment_method_line.id,
            "memo": txn.transaction_reference or txn.description or f"WHMCS transaction {txn.whmcs_transaction_id}",
        }
        if txn.currency:
            currency = self._resolve_currency(txn.currency)
            if currency:
                vals["currency_id"] = currency.id
        return vals

    def create_payments_batch(self, chunk):
        """Create and post a chunk of payments with one ORM create call."""
        if not chunk:
            return self.env["account.payment"]
        vals_list = [self.build_payment_vals(txn, partner_id) for txn, partner_id, _invoice_id in chunk]
        _logger.info("Creating payment batch of %s records", len(vals_list))
        payments = self.env["account.payment"].create(vals_list)
        payments.action_post()

        for (txn, _partner_id, invoice_id), payment in zip(chunk, payments):
            if invoice_id:
                self._reconcile_with_invoice(payment, invoice_id)

        _logger.info("Created payment batch: %s records", len(payments))
        return payments

    def _resolve_journal(self, gateway_code: str):
        """Look up gateway → journal mapping; fall back to default payment journal."""
        gateway_key = (gateway_code or "").strip().lower()
        if gateway_key in self._journal_cache:
            return self._journal_cache[gateway_key]
        if gateway_code:
            mapping = self.env["whmcs.gateway.mapping"].search(
                [("gateway_code", "=", gateway_code), ("active", "=", True)],
                limit=1,
            )
            if mapping and mapping.journal_id:
                self._journal_cache[gateway_key] = mapping.journal_id
                return mapping.journal_id

        # Fall back to configured default payment journal
        if self.config and self.config.default_payment_journal_id:
            self._journal_cache[gateway_key] = self.config.default_payment_journal_id
            return self.config.default_payment_journal_id

        # Last resort: first bank or cash journal
        journal = self.env["account.journal"].search(
            [
                ("type", "in", ("bank", "cash")),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        self._journal_cache[gateway_key] = journal
        return journal

    def _resolve_payment_method_line(self, journal):
        """Find the default inbound payment method line for the journal."""
        if not journal:
            return None
        # Prefer 'manual' inbound payment method
        for pml in journal.inbound_payment_method_line_ids:
            if pml.code in ("manual", "bank"):
                return pml
        # Fall back to first available
        if journal.inbound_payment_method_line_ids:
            return journal.inbound_payment_method_line_ids[0]
        return None

    def _resolve_currency(self, currency_code: str):
        if not currency_code:
            return None
        code = currency_code.upper()
        if code in self._currency_cache:
            return self._currency_cache[code]
        currency = self.env["res.currency"].search(
            [("name", "=", code), ("active", "in", [True, False])],
            limit=1,
        )
        if currency and not currency.active:
            currency.active = True
        if currency:
            self._currency_cache[code] = currency
        return currency

    def _reconcile_with_invoice(self, payment, invoice_id: int):
        """
        Attempt to reconcile the payment with an invoice using Odoo's
        standard reconciliation mechanism.
        """
        try:
            invoice = self.env["account.move"].browse(invoice_id)
            if not invoice.exists() or invoice.state != "posted":
                _logger.warning(
                    "Invoice %s is not posted — skipping reconciliation", invoice_id
                )
                return

            # Use Odoo's standard reconciliation
            # account.payment in Odoo 19 creates a journal entry.
            # We use the invoice's `js_assign_outstanding_line` or
            # reconcile the move lines directly.
            receivable_lines = invoice.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable"
                and not l.reconciled
            )
            payment_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable"
                and not l.reconciled
            )
            if receivable_lines and payment_lines:
                (receivable_lines + payment_lines).reconcile()
                _logger.info(
                    "Reconciled payment %s with invoice %s",
                    payment.id, invoice_id,
                )
        except Exception as exc:
            _logger.warning(
                "Reconciliation failed for payment %s / invoice %s: %s",
                payment.id, invoice_id, exc,
            )

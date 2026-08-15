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

from .normalizer import NormalizedTransaction

_logger = logging.getLogger(__name__)


class PaymentImporter:

    def __init__(self, env, config):
        self.env = env
        self.config = config

    def create_payment(
        self,
        txn: NormalizedTransaction,
        partner_id: int,
        invoice_id: int = None,
    ) -> "account.payment":
        """
        Create and post an inbound account.payment.

        :param txn: NormalizedTransaction
        :param partner_id: resolved Odoo res.partner ID
        :param invoice_id: Odoo account.move ID to reconcile with (optional)
        :returns: posted account.payment record
        """
        _logger.info(
            "Creating payment for WHMCS transaction %s, partner %s",
            txn.whmcs_transaction_id, partner_id,
        )

        # Resolve journal from gateway mapping
        journal = self._resolve_journal(txn.gateway)
        if not journal:
            raise ValueError(
                f"No Odoo journal mapped to WHMCS gateway '{txn.gateway}'. "
                f"Configure it in WHMCS Import → Configuration → Payment Gateways."
            )

        # Resolve currency
        currency = self._resolve_currency(txn.currency)

        # Build payment vals
        vals = {
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner_id,
            "amount": txn.amount,
            "date": txn.date or str(date.today()),
            "journal_id": journal.id,
            "ref": txn.transaction_reference or f"WHMCS-TXN-{txn.whmcs_transaction_id}",
        }

        if currency:
            vals["currency_id"] = currency.id

        # Payment method line
        pml = self._resolve_payment_method_line(journal)
        if pml:
            vals["payment_method_line_id"] = pml.id

        # Create the payment record
        payment = self.env["account.payment"].create(vals)
        _logger.info(
            "Created payment id=%s for WHMCS transaction %s",
            payment.id, txn.whmcs_transaction_id,
        )

        # Post the payment (moves it from draft to posted state)
        payment.action_post()
        _logger.info("Posted payment %s", payment.id)

        # Attempt reconciliation with invoice if provided
        if invoice_id:
            self._reconcile_with_invoice(payment, invoice_id)

        return payment

    def _resolve_journal(self, gateway_code: str):
        """Look up gateway → journal mapping; fall back to default payment journal."""
        if gateway_code:
            mapping = self.env["whmcs.gateway.mapping"].search(
                [("gateway_code", "=", gateway_code), ("active", "=", True)],
                limit=1,
            )
            if mapping and mapping.journal_id:
                return mapping.journal_id

        # Fall back to configured default payment journal
        if self.config and self.config.default_payment_journal_id:
            return self.config.default_payment_journal_id

        # Last resort: first bank or cash journal
        return self.env["account.journal"].search(
            [
                ("type", "in", ("bank", "cash")),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )

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
        currency = self.env["res.currency"].search(
            [("name", "=", currency_code.upper()), ("active", "in", [True, False])],
            limit=1,
        )
        if currency and not currency.active:
            currency.active = True
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

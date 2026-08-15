# -*- coding: utf-8 -*-
"""
Invoice Importer.

Creates Odoo account.move (customer invoice) records from NormalizedInvoice objects.

Uses proper Odoo ORM/business API — does NOT manipulate accounting tables directly.
Invoice numbering is left entirely to Odoo's sequence mechanism.
"""
import logging
from datetime import date

from .normalizer import NormalizedInvoice

_logger = logging.getLogger(__name__)


class InvoiceImporter:

    def __init__(self, env, config):
        """
        :param env: Odoo environment
        :param config: whmcs.import.config singleton record
        """
        self.env = env
        self.config = config

    def create_invoice(self, invoice: NormalizedInvoice, partner_id: int) -> "account.move":
        """
        Create and (where appropriate) post an Odoo customer invoice.

        Returns the created account.move record.
        """
        _logger.info(
            "Creating invoice for WHMCS invoice %s, partner %s",
            invoice.whmcs_invoice_id, partner_id,
        )

        # Resolve currency
        currency = self._resolve_currency(invoice.currency)

        # Build invoice header vals
        vals = {
            "move_type": "out_invoice",
            "partner_id": partner_id,
            "invoice_date": invoice.date or str(date.today()),
            "invoice_date_due": invoice.due_date or invoice.date or str(date.today()),
            "currency_id": currency.id if currency else self.env.company.currency_id.id,
            "ref": invoice.invoice_number,            # WHMCS invoice number as reference
            "invoice_line_ids": [],
        }

        # Journal
        journal = self._resolve_journal()
        if journal:
            vals["journal_id"] = journal.id

        # Invoice lines
        invoice_lines = self._build_invoice_lines(invoice)
        vals["invoice_line_ids"] = [(0, 0, line) for line in invoice_lines]

        # Create the move — Odoo generates the internal invoice number/sequence
        move = self.env["account.move"].create(vals)
        _logger.info(
            "Created account.move id=%s name=%s for WHMCS invoice %s",
            move.id, move.name, invoice.whmcs_invoice_id,
        )

        # Post if appropriate (Paid or Unpaid invoices should be confirmed)
        if not invoice.is_cancelled:
            move.action_post()
            _logger.info("Posted invoice %s (WHMCS status: %s)", move.name, invoice.status)

        return move

    def _build_invoice_lines(self, invoice: NormalizedInvoice) -> list:
        """Build account.move.line vals for invoice lines."""
        lines = []

        # Resolve default product
        default_product = self._resolve_default_product()

        # Resolve default tax
        tax_ids = self._resolve_default_taxes()

        if invoice.lines:
            for line in invoice.lines:
                line_vals = {
                    "name": line.description or "WHMCS Service",
                    "quantity": line.quantity,
                    "price_unit": line.unit_price,
                }
                if default_product:
                    line_vals["product_id"] = default_product.id
                if tax_ids:
                    line_vals["tax_ids"] = [(6, 0, tax_ids)]
                lines.append(line_vals)
        else:
            # Fallback: single line from invoice total
            line_vals = {
                "name": f"WHMCS Invoice {invoice.invoice_number or invoice.whmcs_invoice_id}",
                "quantity": 1.0,
                "price_unit": invoice.total,
            }
            if default_product:
                line_vals["product_id"] = default_product.id
            if tax_ids:
                line_vals["tax_ids"] = [(6, 0, tax_ids)]
            lines.append(line_vals)

        return lines

    def _resolve_currency(self, currency_code: str):
        if not currency_code:
            return None
        currency = self.env["res.currency"].search(
            [("name", "=", currency_code.upper()), ("active", "in", [True, False])],
            limit=1,
        )
        if not currency:
            raise ValueError(
                f"Currency '{currency_code}' is not configured in this Odoo database. "
                f"Please activate it in Accounting → Configuration → Currencies."
            )
        if not currency.active:
            _logger.warning("Currency %s is not active — activating for import.", currency_code)
            currency.active = True
        return currency

    def _resolve_journal(self):
        if self.config and self.config.default_invoice_journal_id:
            return self.config.default_invoice_journal_id
        # Fallback: first sale journal
        return self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )

    def _resolve_default_product(self):
        if self.config and self.config.default_product_id:
            return self.config.default_product_id
        # Fallback: search for the demo product by name
        return self.env["product.product"].search(
            [("name", "=", "WHMCS Imported Service")], limit=1
        )

    def _resolve_default_taxes(self) -> list:
        if self.config and self.config.default_tax_id:
            return [self.config.default_tax_id.id]
        return []

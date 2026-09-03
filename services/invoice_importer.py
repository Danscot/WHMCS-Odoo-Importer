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
        self._currency_cache = {}
        self._journal_cache = None
        self._product_cache = None
        self._tax_ids_cache = None

    def create_invoice(self, invoice: NormalizedInvoice, partner_id: int) -> "account.move":
        """Create and post one customer invoice."""
        move = self.env["account.move"].create(self.build_invoice_vals(invoice, partner_id))
        if not invoice.is_cancelled:
            move.action_post()
        _logger.info("Created account.move id=%s name=%s for WHMCS invoice %s",
                     move.id, move.name, invoice.whmcs_invoice_id)
        return move

    def build_invoice_vals(self, invoice: NormalizedInvoice, partner_id: int) -> dict:
        """Build a complete account.move create payload."""
        vals = {
            "move_type": "out_invoice",
            "partner_id": int(partner_id),
            "invoice_line_ids": [(0, 0, line) for line in self._build_invoice_lines(invoice)],
        }

        journal = self._resolve_journal()
        if not journal:
            raise ValueError("No sales journal is configured for WHMCS invoice import.")
        vals["journal_id"] = journal.id

        if invoice.date:
            vals["invoice_date"] = invoice.date
        if invoice.due_date:
            vals["invoice_date_due"] = invoice.due_date
        if invoice.currency:
            currency = self._resolve_currency(invoice.currency)
            vals["currency_id"] = currency.id
        if invoice.invoice_number:
            # Keep the original WHMCS number as a reference; Odoo still owns
            # the official sequence/name.
            vals["ref"] = invoice.invoice_number
        return vals

    def create_invoices_batch(self, chunk):
        """Create and post a chunk of invoices with one ORM create call."""
        if not chunk:
            return self.env["account.move"]
        vals_list = [self.build_invoice_vals(invoice, partner_id) for invoice, partner_id in chunk]
        _logger.info("Creating invoice batch of %s records", len(vals_list))
        moves = self.env["account.move"].create(vals_list)

        # Post the non-cancelled invoices as one recordset. If Odoo rejects the
        # batch (bad account/tax/configuration), ImportEngine rolls back this
        # savepoint and retries the chunk record-by-record.
        to_post = moves
        cancelled_ids = {
            move.id for (invoice, _partner_id), move in zip(chunk, moves)
            if invoice.is_cancelled
        }
        to_post = to_post.filtered(lambda m: m.id not in cancelled_ids)
        if to_post:
            to_post.action_post()

        _logger.info("Created invoice batch: %s records", len(moves))
        return moves

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
        code = currency_code.upper()
        if code in self._currency_cache:
            return self._currency_cache[code]
        currency = self.env["res.currency"].search(
            [("name", "=", code), ("active", "in", [True, False])],
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
        self._currency_cache[code] = currency
        return currency

    def _resolve_journal(self):
        if self._journal_cache:
            return self._journal_cache
        if self.config and self.config.default_invoice_journal_id:
            self._journal_cache = self.config.default_invoice_journal_id
            return self._journal_cache
        # Fallback: first sale journal
        self._journal_cache = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        return self._journal_cache

    def _resolve_default_product(self):
        if self._product_cache is not None:
            return self._product_cache
        if self.config and self.config.default_product_id:
            self._product_cache = self.config.default_product_id
            return self._product_cache
        # Fallback: search for the demo product by name
        self._product_cache = self.env["product.product"].search(
            [("name", "=", "WHMCS Imported Service")], limit=1
        )
        return self._product_cache

    def _resolve_default_taxes(self) -> list:
        if self._tax_ids_cache is not None:
            return self._tax_ids_cache
        if self.config and self.config.default_tax_id:
            self._tax_ids_cache = [self.config.default_tax_id.id]
        else:
            self._tax_ids_cache = []
        return self._tax_ids_cache

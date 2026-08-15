# -*- coding: utf-8 -*-
"""
Idempotency tests.

Prove that importing the same WHMCS export file twice does not
create duplicate partners, invoices or payments.
"""
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from ..services.normalizer import (
    NormalizedClient, NormalizedExport, NormalizedInvoice,
    NormalizedInvoiceLine, NormalizedTransaction,
)
from ..services.import_engine import ImportEngine


@tagged("whmcs", "whmcs_idempotency")
class TestImportIdempotency(TransactionCase):

    def setUp(self):
        super().setUp()
        self.config = self.env["whmcs.import.config"].get_config()

        # Ensure XAF currency is active
        xaf = self.env["res.currency"].search(
            [("name", "=", "XAF"), ("active", "in", [True, False])], limit=1
        )
        if xaf and not xaf.active:
            xaf.active = True

        # Ensure a sales journal exists
        self.sale_journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)], limit=1
        )
        # Ensure a bank journal exists
        self.bank_journal = self.env["account.journal"].search(
            [("type", "in", ["bank", "cash"]), ("company_id", "=", self.env.company.id)], limit=1
        )
        if self.bank_journal:
            self.config.default_payment_journal_id = self.bank_journal

        # Build a minimal test export
        self.export = NormalizedExport(
            clients=[
                NormalizedClient(
                    whmcs_id=8001,
                    company_name="Idempotency Test Co",
                    email="idempotency@test.cm",
                )
            ],
            invoices=[
                NormalizedInvoice(
                    whmcs_invoice_id=8001,
                    whmcs_client_id=8001,
                    invoice_number="INV-IDEM-8001",
                    date="2026-08-01",
                    due_date="2026-08-15",
                    currency="XAF",
                    status="Paid",
                    total=50000,
                    lines=[
                        NormalizedInvoiceLine(
                            description="Test Service",
                            quantity=1,
                            unit_price=50000,
                        )
                    ],
                )
            ],
            transactions=[
                NormalizedTransaction(
                    whmcs_transaction_id=8001,
                    transaction_reference="TXN-IDEM-8001",
                    whmcs_invoice_id=8001,
                    whmcs_client_id=8001,
                    date="2026-08-03",
                    amount=50000,
                    currency="XAF",
                    gateway="manual",
                )
            ],
        )

    def _make_batch(self):
        return self.env["whmcs.import.batch"].create({
            "name": "Test Batch",
            "mode": "import",
            "state": "running",
        })

    # ------------------------------------------------------------------
    # Test 6 — Duplicate invoice prevention
    # ------------------------------------------------------------------

    def test_duplicate_invoice_prevented(self):
        """Importing the same invoice twice creates exactly one account.move."""
        batch1 = self._make_batch()
        engine1 = ImportEngine(self.env, batch1, self.config, dry_run=False)
        engine1.run(self.export)

        invoices_after_first = self.env["account.move"].search(
            [("ref", "=", "INV-IDEM-8001")]
        )
        self.assertEqual(len(invoices_after_first), 1, "First import should create exactly 1 invoice")

        # Import again
        batch2 = self._make_batch()
        engine2 = ImportEngine(self.env, batch2, self.config, dry_run=False)
        engine2.run(self.export)

        invoices_after_second = self.env["account.move"].search(
            [("ref", "=", "INV-IDEM-8001")]
        )
        self.assertEqual(len(invoices_after_second), 1, "Second import must NOT create a duplicate invoice")

    # ------------------------------------------------------------------
    # Test 7 — Duplicate transaction prevention
    # ------------------------------------------------------------------

    def test_duplicate_transaction_prevented(self):
        """Importing the same transaction twice creates exactly one account.payment."""
        batch1 = self._make_batch()
        engine1 = ImportEngine(self.env, batch1, self.config, dry_run=False)
        engine1.run(self.export)

        payments_after_first = self.env["account.payment"].search(
            [("ref", "=", "TXN-IDEM-8001")]
        )
        self.assertEqual(len(payments_after_first), 1, "First import should create exactly 1 payment")

        # Import again
        batch2 = self._make_batch()
        engine2 = ImportEngine(self.env, batch2, self.config, dry_run=False)
        engine2.run(self.export)

        payments_after_second = self.env["account.payment"].search(
            [("ref", "=", "TXN-IDEM-8001")]
        )
        self.assertEqual(len(payments_after_second), 1, "Second import must NOT create a duplicate payment")

    # ------------------------------------------------------------------
    # Test: Duplicate partner prevention
    # ------------------------------------------------------------------

    def test_duplicate_partner_prevented(self):
        """Importing the same client twice must not create duplicate partners."""
        batch1 = self._make_batch()
        engine1 = ImportEngine(self.env, batch1, self.config, dry_run=False)
        engine1.run(self.export, import_invoices=False, import_transactions=False)

        partners = self.env["res.partner"].search([("email", "=", "idempotency@test.cm")])
        self.assertEqual(len(partners), 1)

        # Import again
        batch2 = self._make_batch()
        engine2 = ImportEngine(self.env, batch2, self.config, dry_run=False)
        engine2.run(self.export, import_invoices=False, import_transactions=False)

        partners_after = self.env["res.partner"].search([("email", "=", "idempotency@test.cm")])
        self.assertEqual(len(partners_after), 1, "Must not create duplicate partner on second import")

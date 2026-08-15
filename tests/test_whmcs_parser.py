# -*- coding: utf-8 -*-
"""Tests for the native WHMCS CSV parser."""

from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from ..services.whmcs_parser import WhmcsParser


@tagged("whmcs", "whmcs_parser")
class TestWhmcsParser(TransactionCase):

    def setUp(self):
        super().setUp()
        self.parser = WhmcsParser()

    def test_clients_csv_skips_section_title(self):
        content = (
            "Clients\n"
            "ID,First Name,Last Name,Company Name,Email,Country,Phone Number,Currency\n"
            "1,John,Doe,Example Ltd,john@example.com,CM,+237600000000,XAF\n"
        ).encode()
        result = self.parser.parse_bytes(content, "clients.csv")
        self.assertEqual(len(result.clients), 1)
        self.assertEqual(result.clients[0].whmcs_id, 1)
        self.assertEqual(result.clients[0].display_name, "Example Ltd")
        self.assertEqual(result.clients[0].currency, "XAF")

    def test_invoices_csv_uses_user_id_as_client_id(self):
        content = (
            "Invoices\n"
            "ID,User ID,Client Name,Invoice Number,Creation Date,Due Date,Subtotal,Credit,Tax,Total,Status\n"
            "8,1,John Doe,,2023-08-24,2023-08-24,49000.00,0.00,0.00,49000.00,Paid\n"
        ).encode()
        result = self.parser.parse_bytes(content, "invoices.csv")
        self.assertEqual(len(result.invoices), 1)
        self.assertEqual(result.invoices[0].whmcs_invoice_id, 8)
        self.assertEqual(result.invoices[0].whmcs_client_id, 1)
        self.assertEqual(result.invoices[0].total, 49000.0)

    def test_transactions_csv_maps_amount_in_and_invoice(self):
        content = (
            "Transactions\n"
            "ID,User ID,Client Name,Currency,Payment Method,Date,Invoice ID,Transaction ID,Amount In,Fees,Amount Out\n"
            "1,3,Anthony SAME,XAF,Carte de crédit,2023-07-26 16:31:03,3,txn_abc,14900.00,0.00,0.00\n"
        ).encode()
        result = self.parser.parse_bytes(content, "transactions.csv")
        self.assertEqual(len(result.transactions), 1)
        txn = result.transactions[0]
        self.assertEqual(txn.whmcs_transaction_id, 1)
        self.assertEqual(txn.whmcs_client_id, 3)
        self.assertEqual(txn.whmcs_invoice_id, 3)
        self.assertEqual(txn.amount, 14900.0)
        self.assertTrue(txn.is_inbound)

    def test_csv_bundle_merges_all_sections(self):
        clients = (
            "Clients\n"
            "ID,First Name,Last Name,Company Name,Email\n"
            "1,John,Doe,,john@example.com\n"
        ).encode()
        invoices = (
            "Invoices\n"
            "ID,User ID,Client Name,Invoice Number,Creation Date,Due Date,Subtotal,Credit,Tax,Total,Status\n"
            "8,1,John Doe,,2023-08-24,2023-08-24,100.00,0.00,0.00,100.00,Paid\n"
        ).encode()
        transactions = (
            "Transactions\n"
            "ID,User ID,Client Name,Currency,Payment Method,Date,Invoice ID,Transaction ID,Amount In,Fees,Amount Out\n"
            "1,1,John Doe,XAF,manual,2023-08-24,8,txn_1,100.00,0.00,0.00\n"
        ).encode()

        result = self.parser.parse_uploaded_csvs(
            clients, "clients.csv",
            invoices, "invoices.csv",
            transactions, "transactions.csv",
        )

        self.assertEqual(len(result.clients), 1)
        self.assertEqual(len(result.invoices), 1)
        self.assertEqual(len(result.transactions), 1)

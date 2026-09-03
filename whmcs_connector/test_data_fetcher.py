#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone regression tests for WHMCS API -> importer data compilation."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from whmcs_connector.whmcs_data_fetcher import WhmcsDataFetcher
from services.whmcs_parser import WhmcsParser


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, action, **params):
        self.calls.append((action, params))

        if action == "GetClients":
            return {
                "result": "success", "totalresults": 1,
                "clients": {"client": [{
                    "id": 1001, "firstname": "Marie", "lastname": "Ngo",
                    "companyname": "Demo Company", "email": "m@example.com",
                    "datecreated": "2026-07-01", "status": "Active",
                }]},
            }
        if action == "GetClientsDetails":
            self._assert_clientid(params)
            return {
                "result": "success",
                "client": {
                    "id": 1001, "firstname": "Marie", "lastname": "Ngo",
                    "companyname": "Demo Company", "email": "m@example.com",
                    "address1": "Avenue Kennedy", "address2": "",
                    "city": "Yaounde", "state": "Centre", "postcode": "1000",
                    "country": "CM", "phonenumber": "+237699000001",
                    "tax_id": "M012345678901A", "language": "french",
                    "currency": 1, "currency_code": "XAF",
                    "customfields": [{"id": 7, "value": "MS-123"}],
                },
            }
        if action == "GetCurrencies":
            return {"result": "success", "currencies": {"currency": [
                {"id": 1, "code": "XAF"},
                {"id": 2, "code": "USD"},
            ]}}
        if action == "GetInvoices":
            return {
                "result": "success", "totalresults": 1,
                "invoices": {"invoice": [{
                    "id": 5001, "userid": 1001, "invoicenum": "INV-5001",
                    "date": "2026-07-01", "duedate": "2026-07-15",
                    "datepaid": "2026-07-03 12:00:00", "subtotal": "150000",
                    "credit": "0", "tax": "0", "tax2": "0", "total": "150000",
                    "status": "Paid", "paymentmethod": "manual",
                    "notes": "", "currencycode": "XAF",
                }]},
            }
        if action == "GetInvoice":
            self._assert_invoiceid(params)
            return {
                "result": "success", "invoiceid": 5001, "userid": 1001,
                "invoicenum": "INV-5001", "date": "2026-07-01",
                "duedate": "2026-07-15", "datepaid": "2026-07-03 12:00:00",
                "subtotal": "150000", "credit": "0", "tax": "0", "tax2": "0",
                "total": "150000", "status": "Paid", "paymentmethod": "manual",
                "notes": "", "items": {"item": [{
                    "id": 1, "type": "Hosting", "relid": 77,
                    "description": "Web Hosting — Annual Plan", "amount": "150000", "taxed": 0,
                }]},
            }
        if action == "GetTransactions":
            # The regression test deliberately fails if the old userid filter
            # is used; WHMCS documents clientid for this endpoint.
            if "userid" in params:
                raise AssertionError("GetTransactions must use clientid, not userid")
            return {
                "result": "success", "totalresults": 1,
                "transactions": {"transaction": [{
                    "id": 9001, "userid": 1001, "currency": 0,
                    "gateway": "manual", "date": "2026-07-03 12:00:00",
                    "description": "Bank transfer — INV-5001",
                    "amountin": "150000", "fees": "0", "amountout": "0",
                    "rate": "1", "transid": "TXN-DEMO-9001", "invoiceid": 5001,
                    "refundid": 0,
                }]},
            }
        raise AssertionError(f"Unexpected API action: {action}")

    @staticmethod
    def _assert_clientid(params):
        assert params.get("clientid") == 1001, params

    @staticmethod
    def _assert_invoiceid(params):
        assert params.get("invoiceid") == 5001, params


class TestWhmcsDataFetcher(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.fetcher = WhmcsDataFetcher(self.api, page_size=100, enrich_workers=4)
        self.parser = WhmcsParser()

    def test_client_is_enriched_to_csv_equivalent_shape(self):
        rows = self.fetcher.fetch_clients()
        self.assertEqual(len(rows), 1)
        client = self.parser._parse_client(rows[0])
        self.assertEqual(client.whmcs_id, 1001)
        self.assertEqual(client.phone, "+237699000001")
        self.assertEqual(client.address1, "Avenue Kennedy")
        self.assertEqual(client.tax_id, "M012345678901A")
        self.assertEqual(client.country, "CM")
        self.assertEqual(client.currency, "XAF")
        self.assertIn("GetClientsDetails", [c[0] for c in self.api.calls])

    def test_invoice_is_enriched_with_real_line_items(self):
        rows = self.fetcher.fetch_invoices()
        invoice = self.parser._parse_invoice(rows[0])
        self.assertEqual(invoice.whmcs_invoice_id, 5001)
        self.assertEqual(invoice.whmcs_client_id, 1001)
        self.assertEqual(invoice.total, 150000.0)
        self.assertEqual(invoice.currency, "XAF")
        self.assertEqual(len(invoice.lines), 1)
        self.assertEqual(invoice.lines[0].description, "Web Hosting — Annual Plan")
        self.assertEqual(invoice.lines[0].unit_price, 150000.0)
        self.assertEqual(invoice.lines[0].whmcs_product_id, 77)

    def test_transaction_uses_clientid_and_resolves_currency(self):
        rows = self.fetcher.fetch_transactions(userid=1001)
        txn = self.parser._parse_transaction(rows[0])
        self.assertEqual(txn.whmcs_transaction_id, 9001)
        self.assertEqual(txn.whmcs_client_id, 1001)
        self.assertEqual(txn.whmcs_invoice_id, 5001)
        self.assertEqual(txn.amount, 150000.0)
        self.assertEqual(txn.amount_in, 150000.0)
        self.assertEqual(txn.currency, "XAF")
        self.assertEqual(txn.gateway, "manual")
        self.assertEqual(txn.transaction_reference, "TXN-DEMO-9001")

    def test_incremental_transaction_filter_is_local(self):
        # The fake transaction is on July 3. The API should not receive
        # undocumented datefrom/dateto parameters; filtering happens here.
        rows = self.fetcher.fetch_transactions(date_from="2026-07-04", date_to="2026-07-05")
        self.assertEqual(rows, [])
        tx_calls = [p for a, p in self.api.calls if a == "GetTransactions"]
        self.assertEqual(len(tx_calls), 1)
        self.assertNotIn("datefrom", tx_calls[0])
        self.assertNotIn("dateto", tx_calls[0])

    def test_detail_enrichment_is_deduplicated_and_parallel_safe(self):
        rows = self.fetcher.fetch_clients()
        self.assertEqual(len(rows), 1)
        detail_calls = [c for c in self.api.calls if c[0] == "GetClientsDetails"]
        self.assertEqual(len(detail_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

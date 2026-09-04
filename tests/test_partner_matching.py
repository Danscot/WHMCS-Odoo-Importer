# -*- coding: utf-8 -*-
"""
Tests for PartnerResolver — the 7-level contact matching logic.

Run with:
    odoo-bin -d <db> --test-enable -i whmcs_odoo_import
"""
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from ..services.normalizer import NormalizedClient
from ..services.partner_resolver import PartnerResolver, STATUS_MATCHED, STATUS_NEW, STATUS_AMBIGUOUS


@tagged("whmcs", "whmcs_matching")
class TestPartnerMatching(TransactionCase):

    def setUp(self):
        super().setUp()
        self.resolver = PartnerResolver(self.env)
        self.Partner = self.env["res.partner"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_client(self, **kwargs) -> NormalizedClient:
        defaults = {
            "whmcs_id": 9999,
            "first_name": "Test",
            "last_name": "Client",
            "company_name": "",
            "email": "test@example.com",
            "phone": "",
            "tax_id": "",
        }
        defaults.update(kwargs)
        return NormalizedClient(**defaults)

    def _make_partner(self, **vals):
        vals.setdefault("customer_rank", 1)
        return self.Partner.create(vals)

    # ------------------------------------------------------------------
    # Test 1 — Level 1: WHMCS mapping table
    # ------------------------------------------------------------------

    def test_level1_whmcs_mapping_with_consistent_identity(self):
        """A mapping is strong, but populated identity fields must still agree."""
        partner = self._make_partner(
            name="Mapped Company",
            email="mapped@example.com",
            vat="M012345678901A",
            phone="+237600000001",
        )
        self.env["whmcs.partner.mapping"].create({
            "whmcs_client_id": 1001,
            "partner_id": partner.id,
            "match_method": "whmcs_mapping",
            "confidence": 100,
        })
        client = self._make_client(
            whmcs_id=1001,
            email="mapped@example.com",
            tax_id="M012345678901A",
            phone="237600000001",
        )
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_MATCHED)
        self.assertEqual(result["partner_id"], partner.id)
        self.assertEqual(result["method"], "whmcs_mapping")

    def test_mapping_email_conflict_is_ambiguous(self):
        """A stale mapping must not hide a conflicting populated email."""
        partner = self._make_partner(
            name="Mapped Company",
            email="old@example.com",
            vat="M012345678901A",
        )
        self.env["whmcs.partner.mapping"].create({
            "whmcs_client_id": 1002,
            "partner_id": partner.id,
            "match_method": "whmcs_mapping",
            "confidence": 100,
        })
        client = self._make_client(
            whmcs_id=1002,
            email="new@example.com",
            tax_id="M012345678901A",
        )
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_AMBIGUOUS)
        self.assertIn("email", [c["field"] for c in result["conflicts"]])

    # ------------------------------------------------------------------
    # Test 2 — Level 2: Exact VAT
    # ------------------------------------------------------------------

    def test_level2_vat_match(self):
        """Client with unique matching VAT must be matched."""
        partner = self._make_partner(name="VAT Company", vat="M012345678901A")
        client = self._make_client(whmcs_id=2001, tax_id="M012345678901A", email="")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_MATCHED)
        self.assertEqual(result["partner_id"], partner.id)
        self.assertEqual(result["method"], "vat")

    def test_level2_vat_ambiguous(self):
        """Two partners with the same VAT must produce AMBIGUOUS (data error)."""
        self._make_partner(name="VAT Company A", vat="M999888777666A")
        self._make_partner(name="VAT Company B", vat="M999888777666A")
        client = self._make_client(whmcs_id=2002, tax_id="M999888777666A", email="")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_AMBIGUOUS)

    def test_vat_match_with_conflicting_email_is_ambiguous(self):
        """A VAT hit is not safe when the candidate's populated email differs."""
        partner = self._make_partner(
            name="VAT Company",
            vat="M123456789012A",
            email="odoo@example.com",
        )
        client = self._make_client(
            whmcs_id=2003,
            tax_id="M123456789012A",
            email="whmcs@example.com",
        )
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_AMBIGUOUS)
        self.assertEqual(result["reason"], "identity_conflict")

    def test_two_identifiers_pointing_to_different_partners_is_ambiguous(self):
        """Different identity sources resolving to different partners are ambiguous."""
        vat_partner = self._make_partner(
            name="VAT Partner", vat="M123456789013A"
        )
        email_partner = self._make_partner(
            name="Email Partner", email="same@example.com"
        )
        client = self._make_client(
            whmcs_id=2004,
            tax_id="M123456789013A",
            email="same@example.com",
        )
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_AMBIGUOUS)
        self.assertEqual(
            {c["partner_id"] for c in result["candidates"]},
            {vat_partner.id, email_partner.id},
        )

    # ------------------------------------------------------------------
    # Test 3 — Level 4: Email (skipping Level 3 which requires custom field)
    # ------------------------------------------------------------------

    def test_level4_email_match(self):
        """Client with unique matching email must be matched."""
        partner = self._make_partner(name="Email Company", email="unique@example.com")
        client = self._make_client(whmcs_id=4001, email="unique@example.com")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_MATCHED)
        self.assertEqual(result["partner_id"], partner.id)
        self.assertEqual(result["method"], "email")

    def test_level4_email_case_insensitive(self):
        """Email matching must be case-insensitive."""
        partner = self._make_partner(name="Case Company", email="Test@Example.COM")
        client = self._make_client(whmcs_id=4002, email="test@example.com")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_MATCHED)
        self.assertEqual(result["partner_id"], partner.id)

    def test_level4_email_ambiguous(self):
        """Two partners sharing an email must produce AMBIGUOUS."""
        self._make_partner(name="Dup Email A", email="shared@example.com")
        self._make_partner(name="Dup Email B", email="shared@example.com")
        client = self._make_client(whmcs_id=4003, email="shared@example.com")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_AMBIGUOUS)

    # ------------------------------------------------------------------
    # Test 4 — New client (Level 7)
    # ------------------------------------------------------------------

    def test_level7_new_client(self):
        """Client with no matching partner must return STATUS_NEW."""
        client = self._make_client(
            whmcs_id=7001,
            email="brandnew@nowhere.cm",
            tax_id="ZZZNOBODYMATCHESTHIS",
        )
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_NEW)
        self.assertIsNone(result["partner_id"])

    # ------------------------------------------------------------------
    # Test 5 — Level 5: Phone matching
    # ------------------------------------------------------------------

    def test_level5_phone_match(self):
        """Client with unique normalized phone must be matched."""
        partner = self._make_partner(name="Phone Company", phone="+237 6 55 00 01 01")
        client = self._make_client(whmcs_id=5001, email="", phone="237655000101")
        result = self.resolver.resolve(client)
        self.assertEqual(result["status"], STATUS_MATCHED)
        self.assertEqual(result["partner_id"], partner.id)
        self.assertEqual(result["method"], "phone")

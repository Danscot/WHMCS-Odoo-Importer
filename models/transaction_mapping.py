# -*- coding: utf-8 -*-
"""
WHMCS Transaction Mapping.

Links WHMCS transaction IDs to Odoo account.payment records.
Enables idempotent imports.
"""
from odoo import fields, models


class WhmcsTransactionMapping(models.Model):
    _name = "whmcs.transaction.mapping"
    _description = "WHMCS Transaction → Odoo Payment Mapping"
    _rec_name = "whmcs_transaction_id"
    _order = "whmcs_transaction_id asc"

    whmcs_transaction_id = fields.Integer(
        "WHMCS Transaction ID", required=True, index=True
    )
    transaction_reference = fields.Char("WHMCS Transaction Reference")
    payment_id = fields.Many2one(
        "account.payment",
        string="Odoo Payment",
        ondelete="set null",
    )
    invoice_id = fields.Many2one(
        "account.move",
        string="Linked Invoice",
        ondelete="set null",
    )
    status = fields.Char("Status at Import")
    fingerprint = fields.Char(
        "Dedup Fingerprint",
        help="Fallback dedup key: client|invoice|amount|date|gateway|ref",
    )

    _sql_constraints = [
        (
            "whmcs_transaction_id_unique",
            "UNIQUE(whmcs_transaction_id)",
            "Each WHMCS transaction may only be imported once.",
        )
    ]

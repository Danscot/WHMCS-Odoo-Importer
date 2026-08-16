# -*- coding: utf-8 -*-
"""
WHMCS Invoice Mapping.

Links WHMCS invoice IDs to Odoo account.move records.
Enables idempotent imports — importing the same WHMCS export twice
will find existing mappings and skip creation.
"""
from odoo import fields, models


class WhmcsInvoiceMapping(models.Model):
    _name = "whmcs.invoice.mapping"
    _description = "WHMCS Invoice → Odoo Invoice Mapping"
    _rec_name = "whmcs_invoice_id"
    _order = "whmcs_invoice_id asc"

    whmcs_invoice_id = fields.Integer(
        "WHMCS Invoice ID", required=True, index=True
    )
    invoice_id = fields.Many2one(
        "account.move",
        string="Odoo Invoice",
        ondelete="set null",
    )
    whmcs_invoice_number = fields.Char("WHMCS Invoice Number")
    status = fields.Char("WHMCS Status at Import")

    _whmcs_invoice_id_unique = models.Constraint(
        "UNIQUE(whmcs_invoice_id)",
        "Each WHMCS invoice may only be imported once.",
    )

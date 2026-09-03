# -*- coding: utf-8 -*-
"""
WHMCS Partner Mapping.

Links each WHMCS client ID to an Odoo res.partner record.
Used for Level-1 (strongest) matching on subsequent imports.
"""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class WhmcsPartnerMapping(models.Model):
    _name = "whmcs.partner.mapping"
    _description = "WHMCS Client → Odoo Partner Mapping"
    _rec_name = "whmcs_client_id"
    _order = "whmcs_client_id asc"

    whmcs_client_id = fields.Integer(
        "WHMCS Client ID", required=True, index=True,
        help="The numeric client ID from WHMCS."
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Odoo Partner",
        required=True,
        ondelete="restrict",
        help="The matched or created Odoo contact.",
    )
    odoo_reference = fields.Char(
        "Odoo Reference (STDxxxxxx)",
        related="partner_id.ref",
        store=True,
        readonly=True,
    )
    match_method = fields.Char("Matched By", help="e.g. whmcs_mapping, vat, email, created")
    confidence = fields.Integer("Confidence %", default=100)
    created_by_import = fields.Boolean("Created by Import", default=False)

    _whmcs_client_id_unique = models.Constraint(
        "UNIQUE(whmcs_client_id)",
        "Each WHMCS client ID may only have one Odoo partner mapping.",
    )

    @api.constrains("partner_id")
    def _check_partner_unique(self):
        for rec in self:
            existing = self.search([
                ("partner_id", "=", rec.partner_id.id),
                ("id", "!=", rec.id),
            ])
            if existing:
                raise ValidationError(
                    _(
                        "Partner %s is already mapped to WHMCS client %s.",
                        rec.partner_id.display_name,
                        existing[0].whmcs_client_id,
                    )
                )

# -*- coding: utf-8 -*-
"""
WHMCS Import Configuration.

Singleton configuration record (one per company) holding:
- Default journals, products, taxes
- Gateway → journal mappings
- Matching toggles
- Demo mode flag
"""
import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class WhmcsImportConfig(models.Model):
    _name = "whmcs.import.config"
    _description = "WHMCS Import Configuration"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )

    # ---- General ----
    default_invoice_journal_id = fields.Many2one(
        "account.journal",
        string="Default Invoice Journal",
        domain=[("type", "=", "sale")],
        help="Sales journal used when creating WHMCS invoices.",
    )
    default_payment_journal_id = fields.Many2one(
        "account.journal",
        string="Default Payment Journal",
        domain=[("type", "in", ["bank", "cash"])],
        help="Fallback journal when no gateway mapping is found.",
    )
    default_product_id = fields.Many2one(
        "product.product",
        string="Default Invoice Product",
        domain=[("type", "=", "service")],
        help="Service product used for WHMCS invoice lines.",
    )
    default_tax_id = fields.Many2one(
        "account.tax",
        string="Default Invoice Tax",
        domain=[("type_tax_use", "=", "sale")],
        help="Tax applied to imported invoice lines. Leave empty for no tax.",
    )
    default_currency_id = fields.Many2one(
        "res.currency",
        string="Default Currency",
        default=lambda self: self.env.company.currency_id,
    )

    # ---- Batch processing ----
    client_batch_size = fields.Integer(
        "Client Batch Size", default=250, help="Number of new partners created in one ORM batch."
    )
    invoice_batch_size = fields.Integer(
        "Invoice Batch Size", default=50, help="Number of invoices created in one ORM batch."
    )
    transaction_batch_size = fields.Integer(
        "Transaction Batch Size", default=10, help="Number of payments created in one ORM batch."
    )
    transaction_cron_limit = fields.Integer(
        "Transactions Per Cron Run", default=10,
        help="Maximum WHMCS transactions processed by one cron callback. Keep this small enough to finish well below the Odoo cron timeout."
    )

    # ---- Matching toggles ----
    match_by_whmcs_mapping = fields.Boolean("Match by WHMCS Mapping", default=True)
    match_by_vat = fields.Boolean("Match by VAT / Tax ID", default=True)
    match_by_microsoft_id = fields.Boolean("Match by Microsoft ID", default=True)
    match_by_email = fields.Boolean("Match by Email", default=True)
    match_by_phone = fields.Boolean("Match by Phone", default=True)
    match_by_composite = fields.Boolean("Match by Composite Name+Contact", default=True)

    # ---- Demo mode ----
    demo_mode = fields.Boolean(
        "Demo Mode",
        default=False,
        help="When enabled, use the built-in demo fixture and allow batch resets.",
    )

    # ---- Gateway mappings (one2many) ----
    gateway_mapping_ids = fields.One2many(
        "whmcs.gateway.mapping",
        "config_id",
        string="Payment Gateway Mappings",
    )

    @api.model
    def get_config(self):
        """Return the config for the current company, creating one if needed."""
        config = self.search([("company_id", "=", self.env.company.id)], limit=1)
        if not config:
            config = self.create({"company_id": self.env.company.id})
        return config


class WhmcsGatewayMapping(models.Model):
    _name = "whmcs.gateway.mapping"
    _description = "WHMCS Gateway → Odoo Journal Mapping"
    _rec_name = "gateway_code"

    config_id = fields.Many2one("whmcs.import.config", string="Config", ondelete="cascade")
    gateway_code = fields.Char("WHMCS Gateway Code", required=True, help="e.g. manual, stripe, orangemoney")
    journal_id = fields.Many2one(
        "account.journal",
        string="Odoo Journal",
        domain=[("type", "in", ["bank", "cash"])],
        required=True,
    )
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line",
        string="Payment Method",
        domain="[('journal_id', '=', journal_id)]",
    )
    active = fields.Boolean(default=True)
    notes = fields.Char("Notes")

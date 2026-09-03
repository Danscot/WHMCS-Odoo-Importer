# -*- coding: utf-8 -*-
"""WHMCS Import Configuration and live synchronization controls."""
import logging
import json
import base64
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhmcsImportConfig(models.Model):
    _name = "whmcs.import.config"
    _description = "WHMCS Import Configuration"
    _rec_name = "company_id"

    company_id = fields.Many2one("res.company", string="Company", required=True,
                                 default=lambda self: self.env.company)

    default_invoice_journal_id = fields.Many2one(
        "account.journal", string="Default Invoice Journal",
        domain=[("type", "=", "sale")])
    default_payment_journal_id = fields.Many2one(
        "account.journal", string="Default Payment Journal",
        domain=[("type", "in", ["bank", "cash"])])
    default_product_id = fields.Many2one(
        "product.product", string="Default Invoice Product",
        domain=[("type", "=", "service")])
    default_tax_id = fields.Many2one(
        "account.tax", string="Default Invoice Tax",
        domain=[("type_tax_use", "=", "sale")])
    default_currency_id = fields.Many2one(
        "res.currency", string="Default Currency",
        default=lambda self: self.env.company.currency_id)

    client_batch_size = fields.Integer("Client Batch Size", default=250)
    invoice_batch_size = fields.Integer("Invoice Batch Size", default=50)
    transaction_batch_size = fields.Integer("Transaction Batch Size", default=10)
    transaction_cron_limit = fields.Integer(
        "Transactions Per Cron Run", default=10,
        help="Maximum WHMCS transactions processed by one background callback.")

    auto_sync_enabled = fields.Boolean(
        "Automatic Synchronization", default=False,
        help="Run a WHMCS synchronization automatically on the configured interval.")
    sync_interval_days = fields.Integer(
        "Synchronization Interval (days)", default=2,
        help="Default is every 2 days. The last successful calendar day is included again.")
    sync_import_clients = fields.Boolean("Sync Clients", default=True)
    sync_import_invoices = fields.Boolean("Sync Invoices", default=True)
    sync_import_transactions = fields.Boolean("Sync Transactions", default=True)

    last_sync_at = fields.Datetime(
        "Last Successful Sync", readonly=True,
        help="Incremental syncs start from this calendar day, inclusively.")
    last_sync_started_at = fields.Datetime("Last Sync Started", readonly=True)
    last_sync_finished_at = fields.Datetime("Last Sync Finished", readonly=True)
    last_sync_status = fields.Selection([
        ("never", "Never"), ("running", "Running"), ("success", "Success"),
        ("warning", "Completed with Warnings"), ("failed", "Failed"),
    ], string="Last Sync Status", default="never", readonly=True)
    last_sync_error = fields.Text("Last Sync Error", readonly=True)
    last_sync_batch_id = fields.Many2one(
        "whmcs.import.batch", string="Last Sync Batch", readonly=True)
    initial_sync_done = fields.Boolean(
        "Initial Sync Completed", default=False, readonly=True)

    match_by_whmcs_mapping = fields.Boolean("Match by WHMCS Mapping", default=True)
    match_by_vat = fields.Boolean("Match by VAT / Tax ID", default=True)
    match_by_microsoft_id = fields.Boolean("Match by Microsoft ID", default=True)
    match_by_email = fields.Boolean("Match by Email", default=True)
    match_by_phone = fields.Boolean("Match by Phone", default=True)
    match_by_composite = fields.Boolean("Match by Composite Name+Contact", default=True)

    demo_mode = fields.Boolean("Demo Mode", default=False)
    gateway_mapping_ids = fields.One2many(
        "whmcs.gateway.mapping", "config_id", string="Payment Gateway Mappings")

    @api.model
    def get_config(self):
        config = self.search([("company_id", "=", self.env.company.id)], limit=1)
        if not config:
            config = self.create({"company_id": self.env.company.id})
        return config

    def _check_sync_configuration(self):
        self.ensure_one()
        if self.sync_interval_days < 1:
            raise UserError(_("Synchronization interval must be at least 1 day."))
        from ..services.live_sync import build_client
        try:
            build_client().ping()
        except Exception as exc:
            raise UserError(_("WHMCS connection failed:\n%s") % exc) from exc
        return True

    def action_test_whmcs_connection(self):
        self.ensure_one()
        self._check_sync_configuration()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("WHMCS Connection"),
                "message": _("Connection and authentication succeeded."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_now(self):
        self.ensure_one()
        batch = self._start_live_sync(trigger="manual")
        return {
            "type": "ir.actions.client",
            "tag": "whmcs_sync_progress",
            "params": {
                "batch_id": batch.id if batch else False,
                "config_id": self.id,
            },
        }

    def _start_live_sync(self, trigger="cron"):
        """Create a background batch without fetching WHMCS data in HTTP.

        The previous implementation fetched the complete WHMCS dataset before
        creating the cron record. With ~2k clients, GetClientsDetails enrichment
        can legitimately exceed Odoo's 120s HTTP worker limit. Acquisition is now
        performed by WhmcsImportBatch._run_api_fetch_job in bounded chunks.
        """
        self.ensure_one()
        config = self.sudo()
        running = self.env["whmcs.import.batch"].search([
            ("sync_config_id", "=", config.id),
            ("state", "=", "running"),
        ], limit=1)
        if running:
            _logger.info("WHMCS sync skipped: config %s already has batch %s running", self.id, running.id)
            return running

        now = fields.Datetime.now()
        initial = not config.initial_sync_done or not config.last_sync_at
        sync_from = None if initial else config.last_sync_at
        sync_to = now
        config.write({"last_sync_started_at": now, "last_sync_status": "running", "last_sync_error": False})
        try:
            batch = self.env["whmcs.import.batch"].create({
                "name": (
                    _("WHMCS Initial Live Sync — %s") % now.strftime("%Y-%m-%d %H:%M")
                    if initial else
                    _("WHMCS Live Sync %s → %s") % (sync_from.strftime("%Y-%m-%d"), sync_to.strftime("%Y-%m-%d"))
                ),
                "filename": _("WHMCS API (%s)") % trigger,
                "mode": "import",
                "state": "running",
                "started_at": now,
                "import_clients": config.sync_import_clients,
                "import_invoices": config.sync_import_invoices,
                "import_transactions": config.sync_import_transactions,
                "sync_config_id": config.id,
                "sync_from": sync_from,
                "sync_to": sync_to,
                "sync_initial": initial,
            })
            cron = self.env["ir.cron"].sudo().create({
                "name": _("WHMCS Live Sync Batch %s") % batch.id,
                "model_id": self.env["ir.model"]._get("whmcs.import.batch").id,
                "state": "code",
                "code": "model.browse(%s)._run_import_job()" % batch.id,
                "user_id": self.env.user.id,
                "interval_number": 1,
                "interval_type": "minutes",
                "nextcall": fields.Datetime.now(),
                "active": True,
            })
            batch.sudo().write({"import_cron_id": cron.id})
            return batch
        except Exception as exc:
            config.write({"last_sync_status": "failed", "last_sync_error": str(exc), "last_sync_finished_at": fields.Datetime.now()})
            _logger.exception("Could not queue WHMCS live sync for config %s", self.id)
            raise UserError(_("WHMCS synchronization could not be queued:\n%s") % exc) from exc

    @api.model
    def _cron_sync(self):
        for config in self.search([("auto_sync_enabled", "=", True)]):
            try:
                config._start_live_sync(trigger="automatic")
            except Exception:
                _logger.exception(
                    "Automatic WHMCS sync failed to start for config %s", config.id)

    def write(self, vals):
        result = super().write(vals)
        if any(k in vals for k in ("auto_sync_enabled", "sync_interval_days")):
            cron = self.env.ref(
                "whmcs_odoo_import.ir_cron_whmcs_sync",
                raise_if_not_found=False)
            if cron:
                days = max(1, int(self.sync_interval_days or 2))
                cron.sudo().write({
                    "active": bool(self.auto_sync_enabled),
                    "interval_number": days,
                    "interval_type": "days",
                    "nextcall": fields.Datetime.now() if self.auto_sync_enabled else cron.nextcall,
                })
        return result


class WhmcsGatewayMapping(models.Model):
    _name = "whmcs.gateway.mapping"
    _description = "WHMCS Gateway → Odoo Journal Mapping"
    _rec_name = "gateway_code"

    config_id = fields.Many2one(
        "whmcs.import.config", string="Config", ondelete="cascade")
    gateway_code = fields.Char("WHMCS Gateway Code", required=True)
    journal_id = fields.Many2one(
        "account.journal", string="Odoo Journal",
        domain=[("type", "in", ["bank", "cash"])], required=True)
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line", string="Payment Method",
        domain="[('journal_id', '=', journal_id)]")
    active = fields.Boolean(default=True)
    notes = fields.Char("Notes")

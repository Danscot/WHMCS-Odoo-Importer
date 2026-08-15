# -*- coding: utf-8 -*-
"""
WHMCS Import Batch.

One record per import run. Tracks state, statistics and logs.
"""
import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class WhmcsImportBatch(models.Model):
    _name = "whmcs.import.batch"
    _description = "WHMCS Import Batch"
    _order = "started_at desc"
    _rec_name = "name"

    name = fields.Char("Batch Name", required=True, default="New Import")
    filename = fields.Char("Source File")

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("analyzing", "Analyzing"),
            ("preview", "Preview Ready"),
            ("running", "Importing"),
            ("done", "Done"),
            ("done_with_warnings", "Done with Warnings"),
            ("failed", "Failed"),
        ],
        default="draft",
        string="State",
        # tracking=True requires mail.thread mixin — removed for Odoo 19 compat
    )
    mode = fields.Selection(
        [("dry_run", "Dry Run / Preview"), ("import", "Import")],
        default="dry_run",
        string="Mode",
    )

    started_at = fields.Datetime("Started At")
    finished_at = fields.Datetime("Finished At")

    # ---- Counters ----
    total_clients = fields.Integer("Total Clients")
    matched_clients = fields.Integer("Matched Clients")
    created_clients = fields.Integer("Created Clients")
    ambiguous_clients = fields.Integer("Ambiguous Clients")
    error_clients = fields.Integer("Client Errors")

    total_invoices = fields.Integer("Total Invoices")
    created_invoices = fields.Integer("Created Invoices")
    skipped_invoices = fields.Integer("Skipped Invoices")

    total_transactions = fields.Integer("Total Transactions")
    created_transactions = fields.Integer("Created Transactions")
    skipped_transactions = fields.Integer("Skipped Transactions")

    error_count = fields.Integer("Errors")
    warning_count = fields.Integer("Warnings")

    # ---- Options ----
    import_clients = fields.Boolean("Import Clients", default=True)
    import_invoices = fields.Boolean("Import Invoices", default=True)
    import_transactions = fields.Boolean("Import Transactions", default=True)

    # ---- Logs ----
    log_ids = fields.One2many("whmcs.import.log", "batch_id", string="Import Log")

    # ---- Computed status ----
    log_count = fields.Integer(compute="_compute_log_count", string="Log Entries")
    duration = fields.Float("Duration (s)", compute="_compute_duration", store=True)

    @api.depends("log_ids")
    def _compute_log_count(self):
        for rec in self:
            rec.log_count = len(rec.log_ids)

    @api.depends("started_at", "finished_at")
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration = delta.total_seconds()
            else:
                rec.duration = 0.0

    def action_view_logs(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Import Log",
            "res_model": "whmcs.import.log",
            "view_mode": "list,form",
            "domain": [("batch_id", "=", self.id)],
            "context": {"default_batch_id": self.id},
        }

    def _apply_summary(self, summary: dict):
        """Write engine summary stats to this batch record."""
        self.write({
            "total_clients": summary.get("total_clients", 0),
            "matched_clients": summary.get("matched_clients", 0),
            "created_clients": summary.get("created_clients", 0),
            "ambiguous_clients": summary.get("ambiguous_clients", 0),
            "error_clients": summary.get("error_clients", 0),
            "total_invoices": summary.get("total_invoices", 0),
            "created_invoices": summary.get("created_invoices", 0),
            "skipped_invoices": summary.get("skipped_invoices", 0),
            "total_transactions": summary.get("total_transactions", 0),
            "created_transactions": summary.get("created_transactions", 0),
            "skipped_transactions": summary.get("skipped_transactions", 0),
            "error_count": summary.get("error_count", 0),
            "warning_count": summary.get("warning_count", 0),
        })

        if summary.get("error_count", 0) > 0 or summary.get("warning_count", 0) > 0:
            self.state = "done_with_warnings"
        else:
            self.state = "done"


class WhmcsImportLog(models.Model):
    _name = "whmcs.import.log"
    _description = "WHMCS Import Log Entry"
    _order = "id asc"

    batch_id = fields.Many2one("whmcs.import.batch", string="Batch", ondelete="cascade", required=True)
    entity_type = fields.Selection(
        [("client", "Client"), ("invoice", "Invoice"), ("transaction", "Transaction")],
        string="Entity",
    )
    whmcs_id = fields.Char("WHMCS ID")
    odoo_model = fields.Char("Odoo Model")
    odoo_record_id = fields.Integer("Odoo Record ID")
    action = fields.Selection(
        [
            ("matched", "Matched"),
            ("created", "Created"),
            ("updated", "Updated"),
            ("skipped", "Skipped"),
            ("ambiguous", "Ambiguous"),
            ("error", "Error"),
        ],
        string="Action",
    )
    status = fields.Selection(
        [
            ("ok", "OK"),
            ("preview", "Preview"),
            ("warning", "Warning"),
            ("error", "Error"),
        ],
        string="Status",
    )
    message = fields.Text("Message")
    details = fields.Text("Details")

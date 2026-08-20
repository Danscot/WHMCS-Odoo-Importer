# -*- coding: utf-8 -*-
"""
WHMCS Import Batch.

One record per import run. Tracks state, statistics and logs.
"""
import base64
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

    # Source payload retained for asynchronous imports. Binary fields keep the
    # HTTP request short: the real import is executed by an Odoo cron worker.
    source_json = fields.Binary("Combined WHMCS JSON", attachment=True, readonly=True)
    source_json_filename = fields.Char(readonly=True)
    clients_csv = fields.Binary("Clients CSV", attachment=True, readonly=True)
    clients_csv_filename = fields.Char(readonly=True)
    invoices_csv = fields.Binary("Invoices CSV", attachment=True, readonly=True)
    invoices_csv_filename = fields.Char(readonly=True)
    transactions_csv = fields.Binary("Transactions CSV", attachment=True, readonly=True)
    transactions_csv_filename = fields.Char(readonly=True)
    import_cron_id = fields.Many2one("ir.cron", string="Background Job", readonly=True, ondelete="set null")
    background_setup_done = fields.Boolean(
        "Background Setup Done", default=False, readonly=True,
        help="Partners/invoices have been processed for the asynchronous import."
    )
    transaction_offset = fields.Integer(
        "Transaction Offset", default=0, readonly=True,
        help="Number of WHMCS transactions already handled by background cron runs."
    )

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


    def _run_import_job(self):
        """Process a bounded slice of the import from an Odoo 19 cron worker.

        Odoo 19 cron jobs have a progress API. We deliberately process only a
        small number of WHMCS transactions per cron callback, persist the
        cursor on this batch, commit, and tell the scheduler how much remains.
        The scheduler then invokes this method again ASAP until the import is
        complete. This avoids long-running cron workers and the closed-cursor
        cascade seen when a job exceeds the worker real-time limit.
        """
        self.ensure_one()
        if self.state != "running":
            return

        cron_id = self.env.context.get("cron_id")
        cron = self.env["ir.cron"].browse(cron_id).exists() if cron_id else self.env["ir.cron"]
        try:
            from ..services.whmcs_parser import WhmcsParser
            from ..services.import_engine import ImportEngine

            parser = WhmcsParser()
            if self.source_json:
                export = parser.parse_bytes(
                    base64.b64decode(self.source_json),
                    filename=self.source_json_filename or "export.json",
                )
            else:
                export = parser.parse_uploaded_csvs(
                    clients_content=base64.b64decode(self.clients_csv) if self.clients_csv else None,
                    clients_filename=self.clients_csv_filename or "",
                    invoices_content=base64.b64decode(self.invoices_csv) if self.invoices_csv else None,
                    invoices_filename=self.invoices_csv_filename or "",
                    transactions_content=base64.b64decode(self.transactions_csv) if self.transactions_csv else None,
                    transactions_filename=self.transactions_csv_filename or "",
                )

            config = self.env["whmcs.import.config"].get_config()
            limit = max(1, int(getattr(config, "transaction_cron_limit", 10) or 10))
            offset = max(0, self.transaction_offset)

            engine = ImportEngine(self.env, self, config, dry_run=False)
            summary = engine.run(
                export,
                import_clients=self.import_clients and not self.background_setup_done,
                import_invoices=self.import_invoices and not self.background_setup_done,
                import_transactions=self.import_transactions,
                transaction_offset=offset,
                transaction_limit=limit,
            )

            processed = summary.pop("processed_transaction_count", 0)
            total_transactions = summary.get("total_transactions", len(export.transactions))
            new_offset = min(offset + processed, total_transactions)
            remaining = max(total_transactions - new_offset, 0) if self.import_transactions else 0
            finished = remaining == 0

            # Persist progress before calling the cron progress API.
            vals = {
                "background_setup_done": True,
                "transaction_offset": new_offset,
            }
            if finished:
                vals["finished_at"] = fields.Datetime.now()
            self._apply_summary_increment(summary, finished=finished)
            self.write(vals)

            if cron:
                cron._commit_progress(processed=processed, remaining=remaining, deactivate=finished)
            else:
                self.env.cr.commit()

            _logger.info(
                "WHMCS background import batch %s progress: %s/%s transactions, %s remaining",
                self.id, new_offset, total_transactions, remaining,
            )
        except Exception:
            _logger.exception("WHMCS background import batch %s failed", self.id)
            try:
                self.write({"state": "failed", "finished_at": fields.Datetime.now()})
                self.env.cr.commit()
            except Exception:
                _logger.exception("Could not mark WHMCS batch %s as failed", self.id)
            raise

    def _apply_summary_increment(self, summary: dict, finished=False):
        """Accumulate one background slice into the persistent batch counters."""
        vals = {}
        for key in (
            "total_clients", "matched_clients", "created_clients", "ambiguous_clients",
            "error_clients", "total_invoices", "created_invoices", "skipped_invoices",
            "total_transactions", "created_transactions", "skipped_transactions",
            "error_count", "warning_count",
        ):
            if key == "total_transactions":
                vals[key] = summary.get(key, 0)
            else:
                vals[key] = getattr(self, key) + summary.get(key, 0)

        errors = vals["error_count"]
        warnings = vals["warning_count"]
        vals["state"] = (
            "done_with_warnings" if finished and (errors or warnings)
            else "done" if finished
            else "running"
        )
        self.write(vals)

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

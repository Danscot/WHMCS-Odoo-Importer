# -*- coding: utf-8 -*-
"""
WHMCS Import Wizard.

The wizard is the UI-facing entry point. It:
  1. Accepts the upload file
  2. Parses + analyzes the export (dry run)
  3. Shows the preview summary
  4. Runs the real import on confirmation

All actual import logic lives in services/import_engine.py.
"""
import base64
import logging
from datetime import datetime

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhmcsImportWizard(models.TransientModel):
    _name = "whmcs.import.wizard"
    _description = "WHMCS Import Wizard"

    # ---- Step 1: Upload ----
    state = fields.Selection(
        [
            ("upload", "Upload"),
            ("preview", "Preview"),
            ("done", "Done"),
        ],
        default="upload",
        string="Step",
    )
    # Combined JSON export (legacy/single-file workflow).
    export_file = fields.Binary("Combined WHMCS JSON Export", attachment=False)
    export_filename = fields.Char("JSON Filename")

    # WHMCS also exports Clients, Invoices and Transactions as three
    # independent CSV files.  Keep them separate in the UI so the importer
    # can use the native WHMCS files without asking the user to merge them.
    clients_file = fields.Binary("Clients CSV", attachment=False)
    clients_filename = fields.Char("Clients CSV Filename")
    invoices_file = fields.Binary("Invoices CSV", attachment=False)
    invoices_filename = fields.Char("Invoices CSV Filename")
    transactions_file = fields.Binary("Transactions CSV", attachment=False)
    transactions_filename = fields.Char("Transactions CSV Filename")

    mode = fields.Selection(
        [("dry_run", "Dry Run / Preview"), ("import", "Import")],
        default="dry_run",
        string="Import Mode",
    )
    import_clients = fields.Boolean("Import Clients", default=True)
    import_invoices = fields.Boolean("Import Invoices", default=True)
    import_transactions = fields.Boolean("Import Transactions", default=True)

    # ---- Step 2: Preview results ----
    batch_id = fields.Many2one("whmcs.import.batch", string="Batch", readonly=True)

    # Preview counters (read-only display)
    preview_total_clients = fields.Integer("Clients", readonly=True)
    preview_matched = fields.Integer("Existing", readonly=True)
    preview_new = fields.Integer("New", readonly=True)
    preview_ambiguous = fields.Integer("Ambiguous", readonly=True)
    preview_total_invoices = fields.Integer("Invoices", readonly=True)
    preview_invoices_ready = fields.Integer("Ready", readonly=True)
    preview_invoices_skipped = fields.Integer("Skipped", readonly=True)
    preview_total_txns = fields.Integer("Transactions", readonly=True)
    preview_txns_ready = fields.Integer("Ready", readonly=True)
    preview_txns_skipped = fields.Integer("Skipped", readonly=True)
    preview_errors = fields.Integer("Errors", readonly=True)
    preview_warnings = fields.Integer("Warnings", readonly=True)

    # ---- Step 3: Done ----
    result_message = fields.Text("Result", readonly=True)

    # ------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------

    def _parse_uploaded_export(self):
        """Parse the combined JSON file or any combination of WHMCS CSV files."""
        from ..services.whmcs_parser import WhmcsParser, WhmcsParseError

        parser = WhmcsParser()

        has_json = bool(self.export_file)
        has_csv = any(
            (
                self.clients_file,
                self.invoices_file,
                self.transactions_file,
            )
        )

        if not has_json and not has_csv:
            raise UserError(
                _(
                    "Please upload a combined JSON export or at least one "
                    "WHMCS CSV file (Clients, Invoices or Transactions)."
                )
            )

        if has_json and has_csv:
            raise UserError(
                _(
                    "Use either the combined JSON export OR the CSV files, "
                    "not both at the same time."
                )
            )

        try:
            if has_json:
                return parser.parse_bytes(
                    base64.b64decode(self.export_file),
                    filename=self.export_filename or "export.json",
                )

            return parser.parse_uploaded_csvs(
                clients_content=(
                    base64.b64decode(self.clients_file)
                    if self.clients_file
                    else None
                ),
                clients_filename=self.clients_filename or "",
                invoices_content=(
                    base64.b64decode(self.invoices_file)
                    if self.invoices_file
                    else None
                ),
                invoices_filename=self.invoices_filename or "",
                transactions_content=(
                    base64.b64decode(self.transactions_file)
                    if self.transactions_file
                    else None
                ),
                transactions_filename=self.transactions_filename or "",
            )
        except WhmcsParseError as exc:
            raise UserError(str(exc)) from exc

    def _source_filename(self):
        """Human-readable source name for the import batch."""
        if self.export_filename:
            return self.export_filename

        names = [
            name
            for name in (
                self.clients_filename,
                self.invoices_filename,
                self.transactions_filename,
            )
            if name
        ]
        return ", ".join(names) or "WHMCS CSV export"

    # ------------------------------------------------------------------
    # Step 1 → 2: Analyze
    # ------------------------------------------------------------------

    def action_analyze(self):
        """Parse the export and run a dry-run analysis."""
        self.ensure_one()
        export = self._parse_uploaded_export()
        source_filename = self._source_filename()

        # Create batch record for this analysis
        batch = self.env["whmcs.import.batch"].create({
            "name": f"Import {source_filename} — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "filename": source_filename,
            "mode": "dry_run",
            "state": "analyzing",
            "started_at": fields.Datetime.now(),
            "import_clients": self.import_clients,
            "import_invoices": self.import_invoices,
            "import_transactions": self.import_transactions,
        })

        # Run engine in DRY RUN mode
        config = self.env["whmcs.import.config"].get_config()
        from ..services.import_engine import ImportEngine
        engine = ImportEngine(self.env, batch, config, dry_run=True)
        summary = engine.run(
            export,
            import_clients=self.import_clients,
            import_invoices=self.import_invoices,
            import_transactions=self.import_transactions,
        )

        batch.write({
            "state": "preview",
            "finished_at": fields.Datetime.now(),
            "mode": "dry_run",
        })
        batch._apply_summary(summary)

        # Populate wizard preview fields
        self.write({
            "state": "preview",
            "batch_id": batch.id,
            "preview_total_clients": summary["total_clients"],
            "preview_matched": summary["matched_clients"],
            "preview_new": summary["created_clients"],
            "preview_ambiguous": summary["ambiguous_clients"],
            "preview_total_invoices": summary["total_invoices"],
            "preview_invoices_ready": summary["created_invoices"],
            "preview_invoices_skipped": summary["skipped_invoices"],
            "preview_total_txns": summary["total_transactions"],
            "preview_txns_ready": summary["created_transactions"],
            "preview_txns_skipped": summary["skipped_transactions"],
            "preview_errors": summary["error_count"],
            "preview_warnings": summary["warning_count"],
        })

        # Stay in same wizard, re-render
        return {
            "type": "ir.actions.act_window",
            "res_model": "whmcs.import.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ------------------------------------------------------------------
    # Step 2 → 3: Import
    # ------------------------------------------------------------------

    def action_import(self):
        """Run the real import (creates records in Odoo)."""
        self.ensure_one()
        export = self._parse_uploaded_export()
        source_filename = self._source_filename()

        # Create or reuse batch
        batch = self.batch_id
        if batch:
            batch.write({
                "mode": "import",
                "state": "running",
                "started_at": fields.Datetime.now(),
                "finished_at": False,
            })
            # Clear dry-run logs
            batch.log_ids.unlink()
        else:
            batch = self.env["whmcs.import.batch"].create({
                "name": f"Import {source_filename} — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "filename": source_filename,
                "mode": "import",
                "state": "running",
                "started_at": fields.Datetime.now(),
                "import_clients": self.import_clients,
                "import_invoices": self.import_invoices,
                "import_transactions": self.import_transactions,
            })
            self.batch_id = batch

        config = self.env["whmcs.import.config"].get_config()
        from ..services.import_engine import ImportEngine
        engine = ImportEngine(self.env, batch, config, dry_run=False)

        try:
            summary = engine.run(
                export,
                import_clients=self.import_clients,
                import_invoices=self.import_invoices,
                import_transactions=self.import_transactions,
            )
            batch.write({"finished_at": fields.Datetime.now()})
            batch._apply_summary(summary)

            result_lines = [
                f"✓ {summary['total_clients']} WHMCS clients processed",
                f"✓ {summary['matched_clients']} existing customers matched",
                f"✓ {summary['created_clients']} new customers created",
            ]
            if summary["ambiguous_clients"]:
                result_lines.append(f"⚠ {summary['ambiguous_clients']} customers require review")
            result_lines += [
                f"✓ {summary['total_invoices']} invoices processed",
                f"✓ {summary['created_invoices']} invoices created",
            ]
            if summary["skipped_invoices"]:
                result_lines.append(f"⚠ {summary['skipped_invoices']} invoices skipped")
            result_lines += [
                f"✓ {summary['total_transactions']} transactions processed",
                f"✓ {summary['created_transactions']} payments created",
            ]
            if summary["skipped_transactions"]:
                result_lines.append(f"⚠ {summary['skipped_transactions']} payments skipped")

            self.write({
                "state": "done",
                "result_message": "\n".join(result_lines),
            })

        except Exception as exc:
            batch.write({"state": "failed", "finished_at": fields.Datetime.now()})
            _logger.exception("WHMCS import batch failed")
            raise UserError(
                _("Import failed: %s\n\nCheck the import log for details.") % str(exc)
            ) from exc

        return {
            "type": "ir.actions.act_window",
            "res_model": "whmcs.import.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_view_batch(self):
        """Open the batch record after import."""
        self.ensure_one()
        if not self.batch_id:
            return
        return {
            "type": "ir.actions.act_window",
            "name": _("Import Batch"),
            "res_model": "whmcs.import.batch",
            "res_id": self.batch_id.id,
            "view_mode": "form",
        }

    def action_back(self):
        """Go back to upload step."""
        self.write({"state": "upload"})
        return {
            "type": "ir.actions.act_window",
            "res_model": "whmcs.import.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

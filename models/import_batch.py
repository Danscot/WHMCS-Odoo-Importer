# -*- coding: utf-8 -*-
"""
WHMCS Import Batch.

One record per import run. Tracks state, statistics and logs.
"""
import base64
import json
import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# Demo/API safety limit.
# CSV/manual imports are NOT affected by this limit.
WHMCS_API_IMPORT_LIMIT = 10
WHMCS_API_FETCH_CHUNK = 10


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

    # Payload captured directly from WHMCS for live/automatic synchronization.
    source_api_json = fields.Binary("WHMCS API JSON", attachment=True, readonly=True)
    source_api_filename = fields.Char(readonly=True)

    # Synchronization checkpoint metadata.
    sync_config_id = fields.Many2one(
        "whmcs.import.config", string="Synchronization Configuration",
        readonly=True, ondelete="set null")
    sync_from = fields.Datetime("Sync From", readonly=True)
    sync_to = fields.Datetime("Sync To", readonly=True)
    sync_initial = fields.Boolean("Initial Sync", readonly=True)

    import_cron_id = fields.Many2one("ir.cron", string="Background Job", readonly=True, ondelete="set null")
    background_setup_done = fields.Boolean(
        "Background Setup Done", default=False, readonly=True,
        help="Partners/invoices have been processed for the asynchronous import."
    )
    transaction_offset = fields.Integer(
        "Transaction Offset", default=0, readonly=True,
        help="Number of WHMCS transactions already handled by background cron runs."
    )
    # API acquisition progress is stored inside source_api_json metadata rather
    # than as ORM fields. This is intentional: live-sync deployments can be
    # upgraded by replacing the addon without requiring a database schema
    # migration just for acquisition bookkeeping.

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
        """Run one short background step.

        Live API batches are acquired in chunks first; only once acquisition is
        complete do we enter the normal Odoo import pipeline. This is important
        for large WHMCS databases because even parallel GetClientsDetails calls
        can exceed Odoo's 120s worker real-time limit.
        """
        self.ensure_one()
        if self.state != "running":
            return
        if self.sync_config_id and not self._api_fetch_complete():
            return self._run_api_fetch_job()
        return self._run_import_job_impl()

    def _api_fetch_complete(self):
        """Read acquisition state from the retained JSON payload.

        Kept out of PostgreSQL columns so addon replacement does not produce
        UndefinedColumn errors when the database has not been explicitly
        upgraded yet.
        """
        if not self.source_api_json:
            return False
        try:
            payload = json.loads(base64.b64decode(self.source_api_json).decode("utf-8"))
            return bool((payload.get("_sync") or {}).get("complete"))
        except Exception:
            return False

    def _run_api_fetch_job(self):
        """Fetch exactly one API phase/chunk and persist a checkpoint.

        IMPORTANT: a cron callback must never transition through multiple API
        phases in memory.  The previous implementation fetched the clients,
        changed ``phase`` to invoices, and then fell through to the final
        ``complete=True`` block without ever fetching invoices.  Each phase is
        now its own persisted unit of work:

            cron #1..N -> clients
            cron #N+1..M -> invoices
            cron #M+1..K -> transactions
            final cron -> import into Odoo

        The size is controlled by WHMCS_API_FETCH_CHUNK and the per-entity cap
        by WHMCS_API_IMPORT_LIMIT in the module .env file.
        """
        self.ensure_one()
        from ..services.live_sync import build_client
        from ..whmcs_connector.whmcs_data_fetcher import WhmcsDataFetcher
        from ..whmcs_connector.env_loader import load_dotenv
        load_dotenv()
        config = self.sync_config_id.sudo()
        import os

        try:
            configured_chunk = int(os.environ.get("WHMCS_API_FETCH_CHUNK", str(WHMCS_API_FETCH_CHUNK)))
        except (TypeError, ValueError):
            configured_chunk = WHMCS_API_FETCH_CHUNK
        chunk = max(1, min(1000, configured_chunk))

        try:
            configured_limit = int(os.environ.get("WHMCS_API_IMPORT_LIMIT", str(WHMCS_API_IMPORT_LIMIT)))
        except (TypeError, ValueError):
            configured_limit = WHMCS_API_IMPORT_LIMIT
        api_limit = max(1, min(100000, configured_limit))

        date_from = self.sync_from.strftime("%Y-%m-%d") if self.sync_from and not self.sync_initial else None
        date_to = self.sync_to.strftime("%Y-%m-%d") if self.sync_to and not self.sync_initial else None
        fetcher = WhmcsDataFetcher(build_client())

        payload = {
            "clients": [], "invoices": [], "transactions": [],
            "_sync": {"phase": "clients", "offset": 0, "complete": False},
        }
        if self.source_api_json:
            try:
                payload = json.loads(base64.b64decode(self.source_api_json).decode("utf-8"))
            except Exception as exc:
                _logger.warning("WHMCS batch %s: invalid stored API payload, restarting acquisition: %s", self.id, exc)

        for key in ("clients", "invoices", "transactions"):
            if not isinstance(payload.get(key), list):
                payload[key] = []
        meta = payload.get("_sync")
        if not isinstance(meta, dict):
            meta = {"phase": "clients", "offset": 0, "complete": False}
            payload["_sync"] = meta

        phase = meta.get("phase") or "clients"
        offset = max(0, int(meta.get("offset") or 0))
        phases = ("clients", "invoices", "transactions")

        if phase == "done":
            payload["_sync"] = {"phase": "done", "offset": 0, "complete": True,
                                "limit_reached": bool(meta.get("limit_reached"))}
            self._persist_api_payload(payload)
            return self._cron_progress(processed=1, remaining=1, deactivate=False)

        current = len(payload.get(phase, []))
        if current >= api_limit:
            next_phase = phases[phases.index(phase) + 1] if phase != "transactions" else "done"
            limit_reached = True
            self._sync_log(phase, "", "fetched", "warning",
                           f"API limit reached: {current}/{api_limit}. Advancing to {next_phase}.")
            payload["_sync"] = {
                "phase": next_phase, "offset": 0, "complete": next_phase == "done",
                "limit_reached": limit_reached,
            }
            self._persist_api_payload(payload)
            return self._cron_progress(processed=1, remaining=1, deactivate=False)

        remaining_capacity = api_limit - current
        fetch_size = max(1, min(chunk, remaining_capacity))

        try:
            if phase == "clients":
                records, total = fetcher.fetch_clients_chunk(
                    offset=offset, chunk_size=fetch_size, date_from=date_from, date_to=date_to)
            elif phase == "invoices":
                records, total = fetcher.fetch_invoices_chunk(
                    offset=offset, chunk_size=fetch_size, date_from=date_from, date_to=date_to)
            else:
                records, total = fetcher.fetch_transactions_chunk(
                    offset=offset, chunk_size=fetch_size, date_from=date_from, date_to=date_to)
        except Exception as exc:
            self._sync_log(phase, "", "error", "error", f"API acquisition failed at offset {offset}: {exc}")
            raise

        before = len(payload[phase])
        accepted = records[:remaining_capacity]
        payload[phase].extend(accepted)
        after = len(payload[phase])
        raw_count = len(records)
        has_more = bool(getattr(fetcher, "_last_chunk_has_more", False)) and after < api_limit
        page_end = offset + raw_count

        _logger.info(
            "WHMCS acquisition batch %s: phase=%s offset=%s requested=%s returned=%s total=%s stored=%s/%s has_more=%s",
            self.id, phase, offset, fetch_size, raw_count, total, after, api_limit, has_more,
        )
        self._sync_log(
            phase, "", "fetched", "ok" if raw_count else "warning",
            f"Fetched {raw_count} record(s) from WHMCS (offset={offset}, requested={fetch_size}, "
            f"WHMCS total={total}, stored={after}/{api_limit}, has_more={has_more})."
        )

        if raw_count == 0:
            _logger.warning(
                "WHMCS acquisition batch %s: %s returned 0 records at offset=%s (WHMCS total=%s)",
                self.id, phase, offset, total,
            )

        # There is always a persisted checkpoint between phases.  Never fall
        # through into the next phase in the same callback.
        if has_more and after < api_limit:
            payload["_sync"] = {
                "phase": phase, "offset": page_end, "complete": False,
                "limit_reached": False,
            }
            self._persist_api_payload(payload)
            return self._cron_progress(processed=max(1, raw_count), remaining=1, deactivate=False)

        next_phase = phases[phases.index(phase) + 1] if phase != "transactions" else "done"
        limit_reached = after >= api_limit
        payload["_sync"] = {
            "phase": next_phase, "offset": 0, "complete": next_phase == "done",
            "limit_reached": limit_reached,
        }
        self._sync_log(
            phase, "", "updated", "ok",
            f"Completed {phase} phase: stored {after} record(s). Next phase: {next_phase}."
        )
        self._persist_api_payload(payload)
        return self._cron_progress(processed=max(1, raw_count), remaining=1, deactivate=False)

    def _persist_api_payload(self, payload):
        """Persist the acquisition checkpoint without exposing credentials."""
        self.write({
            "source_api_json": base64.b64encode(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ),
            "source_api_filename": "whmcs-live-sync.json",
        })
        self.env.cr.commit()

    def _sync_log(self, entity_type, whmcs_id, action, status, message):
        """Write an acquisition event into the same batch log used by import."""
        try:
            self.env["whmcs.import.log"].create({
                "batch_id": self.id,
                "entity_type": entity_type if entity_type in ("client", "invoice", "transaction") else False,
                "whmcs_id": str(whmcs_id or ""),
                "action": action,
                "status": status,
                "message": message,
            })
        except Exception:
            _logger.exception("WHMCS batch %s: failed to write acquisition log", self.id)

    def _cron_progress(self, processed=0, remaining=1, deactivate=False):
        cron_id = self.env.context.get("cron_id")
        cron = self.env["ir.cron"].browse(cron_id).exists() if cron_id else self.env["ir.cron"]
        if cron:
            cron._commit_progress(processed=processed, remaining=remaining, deactivate=deactivate)
        return True

    def _run_import_job_impl(self):
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
            if self.source_api_json:
                export = parser.parse_bytes(
                    base64.b64decode(self.source_api_json),
                    filename=self.source_api_filename or "whmcs-live-sync.json",
                )
            elif self.source_json:
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

            if finished and self.sync_config_id:
                sync_config = self.sync_config_id.sudo()
                sync_config.write({
                    "last_sync_at": self.sync_to or fields.Datetime.now(),
                    "last_sync_finished_at": fields.Datetime.now(),
                    "last_sync_status": (
                        "warning"
                        if (self.error_count or self.warning_count)
                        else "success"
                    ),
                    "last_sync_error": False,
                    "last_sync_batch_id": self.id,
                    "initial_sync_done": True,
                })

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
            ("fetched", "Fetched"),
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
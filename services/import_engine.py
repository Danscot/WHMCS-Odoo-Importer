# -*- coding: utf-8 -*-
"""
Import Engine.

Orchestrates the full WHMCS → Odoo import pipeline:

    Parse → Normalize → Resolve partners → Import partners
         → Import invoices → Import payments → Log results

Supports both DRY RUN (read-only analysis) and IMPORT modes.
One bad record never destroys the entire batch — errors are caught,
logged, and the batch continues.
"""
import logging

from .normalizer import NormalizedExport
from .partner_resolver import PartnerResolver, STATUS_MATCHED, STATUS_NEW, STATUS_AMBIGUOUS
from .partner_importer import PartnerImporter
from .invoice_importer import InvoiceImporter
from .payment_importer import PaymentImporter

_logger = logging.getLogger(__name__)


class ImportEngine:

    def __init__(self, env, batch, config, dry_run=True):
        """
        :param env:      Odoo environment
        :param batch:    whmcs.import.batch record (for logging)
        :param config:   whmcs.import.config singleton
        :param dry_run:  If True, analyse only — create nothing
        """
        self.env = env
        self.batch = batch
        self.config = config
        self.dry_run = dry_run

        self.resolver = PartnerResolver(env)
        self.partner_importer = PartnerImporter(env)
        self.invoice_importer = InvoiceImporter(env, config)
        self.payment_importer = PaymentImporter(env, config)

        # Runtime accumulators
        self.partner_results = {}     # whmcs_client_id → resolution dict
        self.invoice_results = {}     # whmcs_invoice_id → {"status", "move_id"}
        self.transaction_results = {} # whmcs_transaction_id → {"status", "payment_id"}

        # Savepoint counter (for unique names)
        self._sp_counter = 0

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, export: NormalizedExport,
            import_clients=True,
            import_invoices=True,
            import_transactions=True,
            transaction_offset=0,
            transaction_limit=None) -> dict:
        """
        Run the import pipeline.

        Returns a summary dict used to populate the batch record.
        """
        mode = "DRY RUN" if self.dry_run else "IMPORT"
        _logger.info("WHMCS Import Engine starting [%s]", mode)

        summary = {
            "total_clients": 0, "matched_clients": 0, "created_clients": 0,
            "ambiguous_clients": 0, "error_clients": 0,
            "total_invoices": 0, "created_invoices": 0, "skipped_invoices": 0,
            "total_transactions": 0, "created_transactions": 0, "skipped_transactions": 0,
            "error_count": 0, "warning_count": 0,
        }

        # ---- PHASE 1: Partners ----
        if import_clients:
            self._process_partners(export, summary)

        # ---- PHASE 2: Invoices ----
        if import_invoices:
            self._process_invoices(export, summary)

        # ---- PHASE 3: Transactions ----
        if import_transactions:
            self._process_transactions(
                export, summary,
                transaction_offset=transaction_offset,
                transaction_limit=transaction_limit,
            )

        _logger.info("WHMCS Import Engine finished [%s] — %s", mode, summary)
        return summary

    # ------------------------------------------------------------------
    # Savepoint helpers — compatible with Odoo 17-19
    # In Odoo 17+, env.cr.savepoint() is a context manager.
    # We use explicit SQL savepoints for fine-grained per-record control.
    # ------------------------------------------------------------------

    def _sp_name(self):
        """Generate a unique savepoint name."""
        self._sp_counter += 1
        return f"whmcs_import_sp_{self._sp_counter}"

    def _savepoint_begin(self, name):
        """Create a SQL savepoint."""
        self.env.cr.execute(f"SAVEPOINT {name}")

    def _savepoint_release(self, name):
        """Release (commit) a SQL savepoint."""
        self.env.cr.execute(f"RELEASE SAVEPOINT {name}")

    def _savepoint_rollback(self, name):
        """Roll back to a SQL savepoint."""
        self.env.cr.execute(f"ROLLBACK TO SAVEPOINT {name}")
        # After rollback, invalidate the ORM cache so it reflects DB state
        self.env.cache.invalidate()

    def _commit_chunk(self):
        """Commit completed import work so large imports do not hold one huge transaction."""
        self.env.cr.commit()
        self.env.cache.invalidate()

    # ------------------------------------------------------------------
    # Phase 1 — Partners
    # ------------------------------------------------------------------

    def _process_partners(self, export: NormalizedExport, summary: dict):
        """Resolve all clients and create NEW partners in controlled ORM batches."""
        summary["total_clients"] = len(export.clients)
        new_clients = []

        for client in export.clients:
            sp_name = self._sp_name()
            try:
                self._savepoint_begin(sp_name)
                resolution = self.resolver.resolve(client)
                self.partner_results[client.whmcs_id] = resolution
                status = resolution["status"]

                if status == STATUS_MATCHED:
                    summary["matched_clients"] += 1
                    self._log("client", client.whmcs_id, "matched", "ok",
                              f"Matched by {resolution['method']}")
                elif status == STATUS_AMBIGUOUS:
                    summary["ambiguous_clients"] += 1
                    summary["warning_count"] += 1
                    candidates = resolution.get("candidates", [])
                    refs = ", ".join(c.get("ref") or c.get("name", "") for c in candidates)
                    self._log("client", client.whmcs_id, "ambiguous", "warning",
                              f"Ambiguous: {len(candidates)} candidates — {refs}")
                elif status == STATUS_NEW:
                    new_clients.append((client, resolution))
                self._savepoint_release(sp_name)
            except Exception as exc:
                try: self._savepoint_rollback(sp_name)
                except Exception: pass
                summary["error_clients"] += 1
                summary["error_count"] += 1
                self.partner_results[client.whmcs_id] = {
                    "status": "error", "partner_id": None, "method": "error", "confidence": 0
                }
                self._log("client", client.whmcs_id, "error", "error", str(exc))

        if self.dry_run:
            for client, resolution in new_clients:
                # Temporary in-memory identity; never passed to ORM.
                resolution["partner_id"] = f"preview:partner:{client.whmcs_id}"
                summary["created_clients"] += 1
                self._log("client", client.whmcs_id, "created", "preview",
                          "[DRY RUN] Would create new partner")
            return

        batch_size = max(1, int(getattr(self.config, "client_batch_size", 250) or 250))
        for offset in range(0, len(new_clients), batch_size):
            chunk = new_clients[offset:offset + batch_size]
            try:
                clients = [x[0] for x in chunk]
                partners = self.partner_importer.create_partners_batch(clients)
                for (client, resolution), partner in zip(chunk, partners):
                    resolution["partner_id"] = partner.id
                    self.env["whmcs.partner.mapping"].create({
                        "whmcs_client_id": client.whmcs_id,
                        "partner_id": partner.id,
                        "match_method": "created",
                        "confidence": 100,
                        "created_by_import": True,
                    })
                    summary["created_clients"] += 1
                    self._log("client", client.whmcs_id, "created", "ok",
                              f"Created Odoo partner {partner.ref or '(ref pending)'}",
                              partner.id)
                self._commit_chunk()
            except Exception as batch_exc:
                # A bad record must not poison the entire import. Fall back to
                # individual savepoints for this chunk.
                _logger.exception("Partner batch failed; falling back to individual records")
                for client, resolution in chunk:
                    sp = self._sp_name()
                    try:
                        self._savepoint_begin(sp)
                        partner = self.partner_importer.create_partner(client)
                        resolution["partner_id"] = partner.id
                        self.env["whmcs.partner.mapping"].create({
                            "whmcs_client_id": client.whmcs_id,
                            "partner_id": partner.id,
                            "match_method": "created",
                            "confidence": 100,
                            "created_by_import": True,
                        })
                        summary["created_clients"] += 1
                        self._log("client", client.whmcs_id, "created", "ok",
                                  f"Created Odoo partner {partner.ref or '(ref pending)'}", partner.id)
                        self._savepoint_release(sp)
                        self._commit_chunk()
                    except Exception as exc:
                        try: self._savepoint_rollback(sp)
                        except Exception: pass
                        summary["error_clients"] += 1
                        summary["error_count"] += 1
                        resolution["status"] = "error"
                        resolution["partner_id"] = None
                        self._log("client", client.whmcs_id, "error", "error", str(exc))

    def _partner_id_for_whmcs_client(self, whmcs_client_id):
        """
        Resolve a WHMCS client ID for invoices/transactions.

        When the same import contains Clients, _process_partners has already
        populated partner_results.  When the user uploads only an Invoices or
        Transactions CSV later, fall back to the persistent WHMCS mapping table.
        """
        if whmcs_client_id is None:
            return None

        result = self.partner_results.get(whmcs_client_id)
        if result and result.get("partner_id"):
            return result["partner_id"]

        mapping = self.env["whmcs.partner.mapping"].search(
            [("whmcs_client_id", "=", whmcs_client_id)],
            limit=1,
        )
        if mapping and mapping.partner_id:
            self.partner_results[whmcs_client_id] = {
                "status": STATUS_MATCHED,
                "partner_id": mapping.partner_id.id,
                "method": "whmcs_mapping",
                "confidence": 100,
            }
            return mapping.partner_id.id

        return None

    # ------------------------------------------------------------------
    # Phase 2 — Invoices
    # ------------------------------------------------------------------

    def _process_invoices(self, export: NormalizedExport, summary: dict):
        """Import invoices in controlled ORM batches; posting remains per move."""
        summary["total_invoices"] = len(export.invoices)
        ready = []

        for invoice in export.invoices:
            try:
                existing = self.env["whmcs.invoice.mapping"].search(
                    [("whmcs_invoice_id", "=", invoice.whmcs_invoice_id)], limit=1)
                if existing:
                    summary["skipped_invoices"] += 1
                    self.invoice_results[invoice.whmcs_invoice_id] = {
                        "status": "existing",
                        "move_id": existing.invoice_id.id if existing.invoice_id else None,
                    }
                    self._log("invoice", invoice.whmcs_invoice_id, "skipped", "ok",
                              "Already imported", existing.invoice_id.id if existing.invoice_id else None)
                    continue

                partner_id = self._partner_id_for_whmcs_client(invoice.whmcs_client_id)
                if not partner_id:
                    status = self.partner_results.get(invoice.whmcs_client_id, {}).get("status", "unknown")
                    summary["skipped_invoices"] += 1
                    summary["warning_count"] += 1
                    self.invoice_results[invoice.whmcs_invoice_id] = {"status": "skipped", "move_id": None}
                    self._log("invoice", invoice.whmcs_invoice_id, "skipped", "warning",
                              f"No resolved Odoo partner (client status: {status}). Skipped.")
                    continue

                if self.dry_run:
                    self.invoice_results[invoice.whmcs_invoice_id] = {
                        "status": "preview", "move_id": f"preview:invoice:{invoice.whmcs_invoice_id}"
                    }
                    summary["created_invoices"] += 1
                    self._log("invoice", invoice.whmcs_invoice_id, "created", "preview",
                              f"[DRY RUN] Would create invoice for partner {partner_id}")
                else:
                    ready.append((invoice, partner_id))
            except Exception as exc:
                summary["skipped_invoices"] += 1
                summary["error_count"] += 1
                self.invoice_results[invoice.whmcs_invoice_id] = {"status": "error", "move_id": None}
                self._log("invoice", invoice.whmcs_invoice_id, "error", "error", str(exc))

        if self.dry_run:
            return

        batch_size = max(1, int(getattr(self.config, "invoice_batch_size", 50) or 50))
        for offset in range(0, len(ready), batch_size):
            chunk = ready[offset:offset + batch_size]
            sp = self._sp_name()
            try:
                self._savepoint_begin(sp)
                moves = self.invoice_importer.create_invoices_batch(chunk)
                for (invoice, _partner_id), move in zip(chunk, moves):
                    self.env["whmcs.invoice.mapping"].create({
                        "whmcs_invoice_id": invoice.whmcs_invoice_id,
                        "invoice_id": move.id,
                        "whmcs_invoice_number": invoice.invoice_number,
                        "status": invoice.status,
                    })
                    summary["created_invoices"] += 1
                    self.invoice_results[invoice.whmcs_invoice_id] = {"status": "created", "move_id": move.id}
                    self._log("invoice", invoice.whmcs_invoice_id, "created", "ok",
                              f"Created {move.name}", move.id)
                self._savepoint_release(sp)
                self._commit_chunk()
            except Exception:
                try: self._savepoint_rollback(sp)
                except Exception: pass
                _logger.exception("Invoice batch failed; falling back to individual invoices")
                for invoice, partner_id in chunk:
                    sp2 = self._sp_name()
                    try:
                        self._savepoint_begin(sp2)
                        move = self.invoice_importer.create_invoice(invoice, partner_id)
                        self.env["whmcs.invoice.mapping"].create({
                            "whmcs_invoice_id": invoice.whmcs_invoice_id,
                            "invoice_id": move.id,
                            "whmcs_invoice_number": invoice.invoice_number,
                            "status": invoice.status,
                        })
                        summary["created_invoices"] += 1
                        self.invoice_results[invoice.whmcs_invoice_id] = {"status": "created", "move_id": move.id}
                        self._log("invoice", invoice.whmcs_invoice_id, "created", "ok",
                                  f"Created {move.name}", move.id)
                        self._savepoint_release(sp2)
                        self._commit_chunk()
                    except Exception as exc:
                        try: self._savepoint_rollback(sp2)
                        except Exception: pass
                        summary["skipped_invoices"] += 1
                        summary["error_count"] += 1
                        self.invoice_results[invoice.whmcs_invoice_id] = {"status": "error", "move_id": None}
                        self._log("invoice", invoice.whmcs_invoice_id, "error", "error", str(exc))

    # ------------------------------------------------------------------
    # Phase 3 — Transactions
    # ------------------------------------------------------------------

    def _process_transactions(self, export: NormalizedExport, summary: dict,
                              transaction_offset=0, transaction_limit=None):
        """Import a bounded slice of positive WHMCS payments.

        The offset/limit pair is persisted by the background batch model so
        each cron callback handles a small, independent unit of work.
        """
        all_transactions = export.transactions
        summary["total_transactions"] = len(all_transactions)
        transaction_offset = max(0, int(transaction_offset or 0))
        if transaction_limit is None:
            transactions = all_transactions[transaction_offset:]
        else:
            transaction_limit = max(1, int(transaction_limit))
            transactions = all_transactions[transaction_offset:transaction_offset + transaction_limit]

        summary["processed_transaction_count"] = len(transactions)
        ready = []

        for txn in transactions:
            try:
                existing = self.env["whmcs.transaction.mapping"].search(
                    [("whmcs_transaction_id", "=", txn.whmcs_transaction_id)], limit=1)
                if existing:
                    summary["skipped_transactions"] += 1
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "existing",
                        "payment_id": existing.payment_id.id if existing.payment_id else None,
                    }
                    self._log("transaction", txn.whmcs_transaction_id, "skipped", "ok",
                              "Already imported")
                    continue

                partner_id = self._partner_id_for_whmcs_client(txn.whmcs_client_id)
                if not partner_id:
                    summary["skipped_transactions"] += 1
                    summary["warning_count"] += 1
                    self.transaction_results[txn.whmcs_transaction_id] = {"status": "skipped", "payment_id": None}
                    self._log("transaction", txn.whmcs_transaction_id, "skipped", "warning",
                              "No resolved partner. Skipped.")
                    continue

                if txn.amount <= 0:
                    summary["skipped_transactions"] += 1
                    summary["warning_count"] += 1
                    self.transaction_results[txn.whmcs_transaction_id] = {"status": "skipped", "payment_id": None}
                    self._log("transaction", txn.whmcs_transaction_id, "skipped", "warning",
                              "Transaction has no positive Amount In; skipped.")
                    continue

                invoice_result = self.invoice_results.get(txn.whmcs_invoice_id, {})
                odoo_invoice_id = invoice_result.get("move_id")
                if not odoo_invoice_id and txn.whmcs_invoice_id:
                    invoice_mapping = self.env["whmcs.invoice.mapping"].search(
                        [("whmcs_invoice_id", "=", txn.whmcs_invoice_id), ("status", "!=", "error")],
                        limit=1,
                    )
                    if invoice_mapping:
                        odoo_invoice_id = invoice_mapping.invoice_id.id
                if self.dry_run:
                    summary["created_transactions"] += 1
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "preview", "payment_id": f"preview:payment:{txn.whmcs_transaction_id}"
                    }
                    self._log("transaction", txn.whmcs_transaction_id, "created", "preview",
                              f"[DRY RUN] Would create payment via gateway '{txn.gateway}'")
                else:
                    # Never pass a preview ID into Odoo ORM.
                    if isinstance(odoo_invoice_id, str) and odoo_invoice_id.startswith("preview:"):
                        odoo_invoice_id = None
                    ready.append((txn, partner_id, odoo_invoice_id))
            except Exception as exc:
                if self._is_dead_cursor_error(exc):
                    _logger.exception("Transaction preparation aborted because the PostgreSQL cursor/connection is no longer usable")
                    raise
                summary["skipped_transactions"] += 1
                summary["error_count"] += 1
                self.transaction_results[txn.whmcs_transaction_id] = {"status": "error", "payment_id": None}
                self._log("transaction", txn.whmcs_transaction_id, "error", "error", str(exc))

        if self.dry_run:
            return

        batch_size = max(1, int(getattr(self.config, "transaction_batch_size", 10) or 10))
        for offset in range(0, len(ready), batch_size):
            chunk = ready[offset:offset + batch_size]
            sp = self._sp_name()
            try:
                self._savepoint_begin(sp)
                payments = self.payment_importer.create_payments_batch(chunk)
                for (txn, _partner_id, invoice_id), payment in zip(chunk, payments):
                    self.env["whmcs.transaction.mapping"].create({
                        "whmcs_transaction_id": txn.whmcs_transaction_id,
                        "transaction_reference": txn.transaction_reference,
                        "payment_id": payment.id,
                        "invoice_id": invoice_id,
                        "status": "posted",
                        "fingerprint": txn.fingerprint,
                    })
                    summary["created_transactions"] += 1
                    self.transaction_results[txn.whmcs_transaction_id] = {"status": "created", "payment_id": payment.id}
                    self._log("transaction", txn.whmcs_transaction_id, "created", "ok",
                              f"Payment posted and reconciled (ref: {txn.transaction_reference})", payment.id)
                self._savepoint_release(sp)
                self._commit_chunk()
            except Exception as batch_exc:
                if self._is_dead_cursor_error(batch_exc):
                    _logger.exception("Payment batch aborted because the PostgreSQL cursor/connection is no longer usable")
                    raise
                try: self._savepoint_rollback(sp)
                except Exception: pass
                _logger.exception("Payment batch failed; falling back to individual payments")
                for txn, partner_id, invoice_id in chunk:
                    sp2 = self._sp_name()
                    try:
                        self._savepoint_begin(sp2)
                        payment = self.payment_importer.create_payment(txn, partner_id, invoice_id=invoice_id)
                        self.env["whmcs.transaction.mapping"].create({
                            "whmcs_transaction_id": txn.whmcs_transaction_id,
                            "transaction_reference": txn.transaction_reference,
                            "payment_id": payment.id,
                            "invoice_id": invoice_id,
                            "status": "posted",
                            "fingerprint": txn.fingerprint,
                        })
                        summary["created_transactions"] += 1
                        self.transaction_results[txn.whmcs_transaction_id] = {"status": "created", "payment_id": payment.id}
                        self._log("transaction", txn.whmcs_transaction_id, "created", "ok",
                                  f"Payment posted and reconciled (ref: {txn.transaction_reference})", payment.id)
                        self._savepoint_release(sp2)
                        self._commit_chunk()
                    except Exception as exc:
                        if self._is_dead_cursor_error(exc):
                            _logger.exception("Individual payment aborted because the PostgreSQL cursor/connection is no longer usable")
                            raise
                        try: self._savepoint_rollback(sp2)
                        except Exception: pass
                        summary["skipped_transactions"] += 1
                        summary["error_count"] += 1
                        self.transaction_results[txn.whmcs_transaction_id] = {"status": "error", "payment_id": None}
                        self._log("transaction", txn.whmcs_transaction_id, "error", "error", str(exc))

    @staticmethod
    def _is_dead_cursor_error(exc):
        """Return True when the DB cursor/connection itself is unusable."""
        current = exc
        while current:
            if current.__class__.__name__ == "InterfaceError" and "cursor already closed" in str(current).lower():
                return True
            current = current.__cause__ or current.__context__
        return False

    # ------------------------------------------------------------------
    # Logging helper
    # ------------------------------------------------------------------

    def _log(self, entity_type, whmcs_id, action, status, message="", odoo_record_id=None):
        if not self.batch:
            return
        try:
            self.env["whmcs.import.log"].create({
                "batch_id": self.batch.id,
                "entity_type": entity_type,
                "whmcs_id": str(whmcs_id),
                "action": action,
                "status": status,
                "message": message,
                "odoo_record_id": odoo_record_id,
            })
        except Exception:
            _logger.exception("Failed to write import log entry")

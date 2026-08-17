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
            import_transactions=True) -> dict:
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
            self._process_transactions(export, summary)

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

    # ------------------------------------------------------------------
    # Phase 1 — Partners
    # ------------------------------------------------------------------

    def _process_partners(self, export: NormalizedExport, summary: dict):
        summary["total_clients"] = len(export.clients)

        for client in export.clients:
            sp_name = self._sp_name()
            try:
                self._savepoint_begin(sp_name)
                resolution = self.resolver.resolve(client)
                self.partner_results[client.whmcs_id] = resolution

                if resolution["status"] == STATUS_MATCHED:
                    summary["matched_clients"] += 1
                    self._log(
                        entity_type="client",
                        whmcs_id=client.whmcs_id,
                        action="matched",
                        status="ok",
                        message=f"Matched by {resolution['method']}",
                    )

                elif resolution["status"] == STATUS_AMBIGUOUS:
                    summary["ambiguous_clients"] += 1
                    summary["warning_count"] += 1
                    candidates = resolution.get("candidates", [])
                    refs = ", ".join(c.get("ref") or c.get("name", "") for c in candidates)
                    self._log(
                        entity_type="client",
                        whmcs_id=client.whmcs_id,
                        action="ambiguous",
                        status="warning",
                        message=f"Ambiguous: {len(candidates)} candidates — {refs}",
                    )

                elif resolution["status"] == STATUS_NEW:
                    if not self.dry_run:
                        partner = self.partner_importer.create_partner(client)
                        resolution["partner_id"] = partner.id
                        # Record mapping
                        self.env["whmcs.partner.mapping"].create({
                            "whmcs_client_id": client.whmcs_id,
                            "partner_id": partner.id,
                            "match_method": "created",
                            "confidence": 100,
                            "created_by_import": True,
                        })
                        summary["created_clients"] += 1
                        self._log(
                            entity_type="client",
                            whmcs_id=client.whmcs_id,
                            action="created",
                            status="ok",
                            odoo_record_id=partner.id,
                            message=f"Created Odoo partner {partner.ref or '(ref pending)'}",
                        )
                    else:
                        # Dry-run must still provide a resolvable partner identity
                        # to downstream invoice/payment preview phases.  We use a
                        # deterministic negative sentinel that can never be a real
                        # Odoo res.partner id.  It lives only in memory and is never
                        # passed to ORM create/write calls.
                        preview_partner_id = self._preview_partner_id(client.whmcs_id)
                        resolution["partner_id"] = preview_partner_id
                        resolution["preview"] = True
                        summary["created_clients"] += 1   # predicted
                        self._log(
                            entity_type="client",
                            whmcs_id=client.whmcs_id,
                            action="created",
                            status="preview",
                            message=f"[DRY RUN] Would create new partner (preview id {preview_partner_id})",
                        )

                self._savepoint_release(sp_name)

            except Exception as exc:
                try:
                    self._savepoint_rollback(sp_name)
                except Exception:
                    pass
                summary["error_clients"] += 1
                summary["error_count"] += 1
                _logger.exception("Error processing WHMCS client %s", client.whmcs_id)
                self.partner_results[client.whmcs_id] = {
                    "status": "error", "partner_id": None,
                    "method": "error", "confidence": 0,
                }
                self._log(
                    entity_type="client",
                    whmcs_id=client.whmcs_id,
                    action="error",
                    status="error",
                    message=str(exc),
                )

    @staticmethod
    def _preview_partner_id(whmcs_client_id):
        """Return a deterministic in-memory-only partner sentinel for Dry Run."""
        try:
            return -abs(int(whmcs_client_id))
        except (TypeError, ValueError):
            # WHMCS IDs are normally integers, but keep the preview path safe if
            # a future export contains a non-numeric identifier.
            return -abs(hash(str(whmcs_client_id)) or 1)

    @staticmethod
    def _preview_invoice_id(whmcs_invoice_id):
        """Return a deterministic in-memory-only invoice sentinel for Dry Run."""
        try:
            return -abs(int(whmcs_invoice_id))
        except (TypeError, ValueError):
            return -abs(hash(str(whmcs_invoice_id)) or 1)

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
        summary["total_invoices"] = len(export.invoices)

        for invoice in export.invoices:
            sp_name = self._sp_name()
            try:
                # Idempotency check
                existing = self.env["whmcs.invoice.mapping"].search(
                    [("whmcs_invoice_id", "=", invoice.whmcs_invoice_id)], limit=1
                )
                if existing:
                    summary["skipped_invoices"] += 1
                    self._log(
                        entity_type="invoice",
                        whmcs_id=invoice.whmcs_invoice_id,
                        action="skipped",
                        status="ok",
                        odoo_record_id=existing.invoice_id.id if existing.invoice_id else None,
                        message="Already imported",
                    )
                    self.invoice_results[invoice.whmcs_invoice_id] = {
                        "status": "existing",
                        "move_id": existing.invoice_id.id if existing.invoice_id else None,
                    }
                    continue

                # Resolve partner
                partner_id = self._partner_id_for_whmcs_client(invoice.whmcs_client_id)
                partner_resolution = self.partner_results.get(invoice.whmcs_client_id, {})

                if not partner_id:
                    reason = partner_resolution.get("status", "unknown")
                    summary["skipped_invoices"] += 1
                    summary["warning_count"] += 1
                    self._log(
                        entity_type="invoice",
                        whmcs_id=invoice.whmcs_invoice_id,
                        action="skipped",
                        status="warning",
                        message=f"No resolved Odoo partner (client status: {reason}). Skipped.",
                    )
                    self.invoice_results[invoice.whmcs_invoice_id] = {
                        "status": "skipped", "move_id": None,
                    }
                    continue

                if self.dry_run:
                    summary["created_invoices"] += 1
                    self._log(
                        entity_type="invoice",
                        whmcs_id=invoice.whmcs_invoice_id,
                        action="created",
                        status="preview",
                        message=f"[DRY RUN] Would create invoice for partner {partner_id}",
                    )
                    # Keep a virtual invoice identity so transactions in the
                    # same Dry Run can resolve their Invoice ID → invoice link.
                    # This sentinel is never sent to Odoo ORM and is discarded
                    # when the engine instance ends.
                    preview_invoice_id = self._preview_invoice_id(invoice.whmcs_invoice_id)
                    self.invoice_results[invoice.whmcs_invoice_id] = {
                        "status": "preview",
                        "move_id": preview_invoice_id,
                        "preview": True,
                    }
                    continue

                self._savepoint_begin(sp_name)
                move = self.invoice_importer.create_invoice(invoice, partner_id)

                # Record mapping for idempotency
                self.env["whmcs.invoice.mapping"].create({
                    "whmcs_invoice_id": invoice.whmcs_invoice_id,
                    "invoice_id": move.id,
                    "whmcs_invoice_number": invoice.invoice_number,
                    "status": invoice.status,
                })
                self._savepoint_release(sp_name)

                summary["created_invoices"] += 1
                self.invoice_results[invoice.whmcs_invoice_id] = {
                    "status": "created", "move_id": move.id,
                }
                self._log(
                    entity_type="invoice",
                    whmcs_id=invoice.whmcs_invoice_id,
                    action="created",
                    status="ok",
                    odoo_record_id=move.id,
                    message=f"Created {move.name}",
                )

            except Exception as exc:
                try:
                    self._savepoint_rollback(sp_name)
                except Exception:
                    pass
                summary["skipped_invoices"] += 1
                summary["error_count"] += 1
                _logger.exception("Error processing WHMCS invoice %s", invoice.whmcs_invoice_id)
                self.invoice_results[invoice.whmcs_invoice_id] = {
                    "status": "error", "move_id": None,
                }
                self._log(
                    entity_type="invoice",
                    whmcs_id=invoice.whmcs_invoice_id,
                    action="error",
                    status="error",
                    message=str(exc),
                )

    # ------------------------------------------------------------------
    # Phase 3 — Transactions
    # ------------------------------------------------------------------

    def _process_transactions(self, export: NormalizedExport, summary: dict):
        summary["total_transactions"] = len(export.transactions)

        for txn in export.transactions:
            sp_name = self._sp_name()
            try:
                # Idempotency check
                existing = self.env["whmcs.transaction.mapping"].search(
                    [("whmcs_transaction_id", "=", txn.whmcs_transaction_id)], limit=1
                )
                if existing:
                    summary["skipped_transactions"] += 1
                    self._log(
                        entity_type="transaction",
                        whmcs_id=txn.whmcs_transaction_id,
                        action="skipped",
                        status="ok",
                        message="Already imported",
                    )
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "existing",
                        "payment_id": existing.payment_id.id if existing.payment_id else None,
                    }
                    continue

                # Resolve partner
                partner_id = self._partner_id_for_whmcs_client(txn.whmcs_client_id)

                if not partner_id:
                    summary["skipped_transactions"] += 1
                    summary["warning_count"] += 1
                    self._log(
                        entity_type="transaction",
                        whmcs_id=txn.whmcs_transaction_id,
                        action="skipped",
                        status="warning",
                        message="No resolved partner. Skipped.",
                    )
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "skipped", "payment_id": None,
                    }
                    continue

                # The WHMCS CSV exposes Amount In and Amount Out.  This Odoo
                # importer creates inbound customer payments, so outbound or
                # zero-value rows must not become fake inbound payments.
                if txn.amount <= 0:
                    summary["skipped_transactions"] += 1
                    summary["warning_count"] += 1
                    self._log(
                        entity_type="transaction",
                        whmcs_id=txn.whmcs_transaction_id,
                        action="skipped",
                        status="warning",
                        message="Transaction has no positive Amount In; skipped.",
                    )
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "skipped", "payment_id": None,
                    }
                    continue

                # Resolve linked invoice (optional)
                invoice_result = self.invoice_results.get(txn.whmcs_invoice_id, {})
                odoo_invoice_id = invoice_result.get("move_id")

                if self.dry_run:
                    summary["created_transactions"] += 1
                    invoice_note = (
                        f" linked to preview invoice {odoo_invoice_id}"
                        if odoo_invoice_id
                        else " (no linked invoice)"
                    )
                    self._log(
                        entity_type="transaction",
                        whmcs_id=txn.whmcs_transaction_id,
                        action="created",
                        status="preview",
                        message=(
                            f"[DRY RUN] Would create payment via gateway '{txn.gateway}'"
                            f"{invoice_note}"
                        ),
                    )
                    self.transaction_results[txn.whmcs_transaction_id] = {
                        "status": "preview", "payment_id": None,
                    }
                    continue

                self._savepoint_begin(sp_name)
                payment = self.payment_importer.create_payment(
                    txn, partner_id, invoice_id=odoo_invoice_id
                )

                # Record mapping for idempotency
                self.env["whmcs.transaction.mapping"].create({
                    "whmcs_transaction_id": txn.whmcs_transaction_id,
                    "transaction_reference": txn.transaction_reference,
                    "payment_id": payment.id,
                    "invoice_id": odoo_invoice_id,
                    "status": "posted",
                    "fingerprint": txn.fingerprint,
                })
                self._savepoint_release(sp_name)

                summary["created_transactions"] += 1
                self.transaction_results[txn.whmcs_transaction_id] = {
                    "status": "created", "payment_id": payment.id,
                }
                self._log(
                    entity_type="transaction",
                    whmcs_id=txn.whmcs_transaction_id,
                    action="created",
                    status="ok",
                    odoo_record_id=payment.id,
                    message=f"Payment posted and reconciled (ref: {txn.transaction_reference})",
                )

            except Exception as exc:
                try:
                    self._savepoint_rollback(sp_name)
                except Exception:
                    pass
                summary["skipped_transactions"] += 1
                summary["error_count"] += 1
                _logger.exception("Error processing WHMCS transaction %s", txn.whmcs_transaction_id)
                self.transaction_results[txn.whmcs_transaction_id] = {
                    "status": "error", "payment_id": None,
                }
                self._log(
                    entity_type="transaction",
                    whmcs_id=txn.whmcs_transaction_id,
                    action="error",
                    status="error",
                    message=str(exc),
                )

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

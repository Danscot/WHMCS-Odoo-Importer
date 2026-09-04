# -*- coding: utf-8 -*-
"""Schema safety migration for WHMCS importer 19.0.2.2.0.

The 19.0.2.1.0 UI introduced configuration fields. Some databases had the
module installed already and therefore did not receive those columns before
the UI attempted to create/update a configuration record.

The ALTER TABLE statements are intentionally idempotent.
"""

from odoo import api, SUPERUSER_ID
from odoo.sql_db import SQL


def migrate(cr, version):
    if not version:
        return

    columns = [
        ("client_batch_size", "integer", "250"),
        ("invoice_batch_size", "integer", "50"),
        ("transaction_batch_size", "integer", "10"),
        ("transaction_cron_limit", "integer", "10"),
        ("api_import_limit", "integer", "500"),
        ("api_fetch_chunk", "integer", "50"),
        ("api_enrich_workers", "integer", "4"),
        ("auto_sync_enabled", "boolean", "false"),
        ("sync_interval_number", "integer", "2"),
        ("sync_interval_type", "varchar", "'days'"),
        ("sync_interval_days", "integer", "2"),
        ("sync_import_clients", "boolean", "true"),
        ("sync_import_invoices", "boolean", "true"),
        ("sync_import_transactions", "boolean", "true"),
        ("last_sync_at", "timestamp", "NULL"),
        ("last_sync_started_at", "timestamp", "NULL"),
        ("last_sync_finished_at", "timestamp", "NULL"),
        ("last_sync_status", "varchar", "'never'"),
        ("last_sync_error", "text", "NULL"),
        ("last_sync_batch_id", "integer", "NULL"),
        ("initial_sync_done", "boolean", "false"),
        ("match_by_whmcs_mapping", "boolean", "true"),
        ("match_by_vat", "boolean", "true"),
        ("match_by_microsoft_id", "boolean", "true"),
        ("match_by_email", "boolean", "true"),
        ("match_by_phone", "boolean", "true"),
        ("match_by_composite", "boolean", "true"),
        ("demo_mode", "boolean", "false"),
    ]

    for name, sql_type, default in columns:
        cr.execute(
            'ALTER TABLE "whmcs_import_config" '
            'ADD COLUMN IF NOT EXISTS "%s" %s DEFAULT %s'
            % (name, sql_type, default)
        )

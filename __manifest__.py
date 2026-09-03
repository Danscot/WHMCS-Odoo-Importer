# -*- coding: utf-8 -*-
{
    "name": "WHMCS → Odoo Importer",
    "version": "19.0.2.0.0",
    "summary": "Import WHMCS clients, invoices and transactions into Odoo",
    "description": """
WHMCS → Odoo Importer
======================
Import clients, invoices and transactions from WHMCS JSON/CSV export files
into Odoo with full duplicate detection, dry-run preview and detailed reporting.

Key features:
- Upload combined JSON or native WHMCS Clients/Invoices/Transactions CSV exports
- Intelligent contact matching (WHMCS mapping, VAT, email, phone, composite)
- Dry Run / Preview mode before committing any data
- Idempotent imports — safe to run the same export multiple times
- Detailed import log and batch history
- Configurable gateway → journal mappings
- Demo mode with sample fixtures
    """,
    "category": "Accounting/Accounting",
    "author": "ST Digital",
    "website": "https://www.st-digital.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "contacts",
        "account",
        "product",
    ],
    "data": [
        # Security first
        "security/security.xml",
        "security/ir.model.access.csv",
        # Data / config
        "data/demo_config.xml",
        "data/whmcs_cron.xml",
        # Views
        "views/whmcs_config_views.xml",
        "views/whmcs_batch_views.xml",
        "views/whmcs_mapping_views.xml",
        "views/whmcs_import_views.xml",
        "views/menus.xml",
    ],
    "demo": [
        "demo/whmcs_demo_export_data.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "assets": {
        "web.assets_backend": [
            "whmcs_odoo_import/static/src/js/whmcs_sync_progress.js",
            "whmcs_odoo_import/static/src/xml/whmcs_sync_progress.xml",
            "whmcs_odoo_import/static/src/css/whmcs_sync_progress.css",
        ],
    },
}

# WHMCS -> Odoo Importer — Odoo 19 installation fix

This package is deliberately wrapped in the required technical module directory:
`whmcs_odoo_import/`.

## Clean installation / upgrade

1. Copy the `whmcs_odoo_import` directory into an Odoo addons path.
2. Make sure there is exactly one module directory level:
   `.../addons/whmcs_odoo_import/__manifest__.py`
3. Restart Odoo completely.
4. Update the Apps list if needed.
5. Upgrade/reinstall the module `WHMCS -> Odoo Importer`.

CLI example:

    odoo -d YOUR_DATABASE -u whmcs_odoo_import --stop-after-init

Then start Odoo normally.

## Important

Do not copy the contents of this directory directly into the addons directory.
The `whmcs_odoo_import` directory itself is the Odoo module.

The server `.env` file is intentionally not bundled in this distribution ZIP.
Create/configure it on the server from the previous environment settings.

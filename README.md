# WHMCS → Odoo Importer

**Module:** `whmcs_odoo_import`  
**Version:** 19.0.1.0.0  
**Target:** Odoo 19 (local development)  
**Author:** ST Digital

---

## Overview

A custom Odoo 19 module that imports WHMCS clients, invoices, and transactions into Odoo with:

- **7-level contact matching** — never creates duplicates
- **Dry Run / Preview** mode before committing any data
- **Idempotent imports** — safe to re-run the same file multiple times
- **Full import report** with per-record log entries
- **Gateway → Journal mapping** for payment routing
- **Demo mode** with built-in fixture data

---

## 1. Requirements

- Odoo 19 (Community or Enterprise)
- Python 3.10+
- PostgreSQL 14+
- Modules: `base`, `contacts`, `account`, `product`
- XAF currency must be activated in Accounting → Configuration → Currencies (for Cameroon demo)

---

## 2. Installation

1. Copy the `whmcs_odoo_import/` directory into your Odoo custom addons path:
   ```
   /path/to/odoo/custom-addons/whmcs_odoo_import/
   ```

2. Restart Odoo:
   ```bash
   ./odoo-bin --addons-path=... -c odoo.conf --stop-after-init
   ./odoo-bin -c odoo.conf
   ```

3. In Odoo, go to **Apps → Update Apps List**.

4. Search for **"WHMCS"** and click **Install** on *WHMCS → Odoo Importer*.

5. The **WHMCS Import** menu will appear in the main navigation bar.

---

## 3. Configuration

Go to **WHMCS Import → Configuration**.

### General

| Setting | Description |
|---------|-------------|
| Default Invoice Journal | Sales journal for imported invoices |
| Default Payment Journal | Fallback bank/cash journal for payments |
| Default Invoice Product | Service product for invoice lines (e.g. *WHMCS Imported Service*) |
| Default Invoice Tax | Tax applied to all imported lines (e.g. 19.25% TVA) |
| Default Currency | Base currency (XAF for Cameroon) |

### Payment Gateway Mappings

Map each WHMCS gateway code to an Odoo journal:

| WHMCS Gateway | Odoo Journal |
|--------------|--------------|
| `manual` | CBC Bank Account Cameroun |
| `orangemoney` | Caisse Mobile Money |
| `stripe` | Stripe |
| `banktransfer` | ECOBANK Cameroun |

### Contact Matching Toggles

Enable/disable each matching level:
- Match by WHMCS Mapping (Level 1 — strongest)
- Match by VAT / Tax ID (Level 2)
- Match by Microsoft ID (Level 3)
- Match by Email (Level 4)
- Match by Phone (Level 5)
- Match by Composite Name+Contact (Level 6)

---

## 4. Demo Setup

The module ships with a demo fixture (`demo/whmcs_demo_export.json`) containing:

| Client | WHMCS ID | Expected Result |
|--------|---------|----------------|
| Demo Existing Company SARL | 1001 | **MATCHED** by VAT or email |
| Demo New Company SARL | 1002 | **CREATED** — Odoo generates STDxxxxxx |
| ABC Cameroon | 1003 | **AMBIGUOUS** — 2 existing Odoo records |
| Kotto Digital SARL | 1004 | **CREATED** |
| Toukour Tech | 1005 | **CREATED** |

When Odoo is started with demo data (`--load-demo-data`), the demo partners are
pre-created automatically.

---

## 5. Importing a File

1. Go to **WHMCS Import → Import Data**.
2. Click **Choose JSON / CSV export** and select your file.
3. Select **Import Mode**:
   - *Dry Run / Preview* — analyse only, create nothing
   - *Import* — commit records to Odoo
4. Check which entity types to import.
5. Click **Analyze Export**.
6. Review the preview summary.
7. Click **Import Now** to commit.

---

## 6. Dry Run / Preview Mode

When **Dry Run** is selected:

- The file is parsed and normalized.
- Contact matching runs against the live database.
- All 7 matching levels are applied.
- A full preview summary is shown (clients / invoices / transactions breakdown).
- **Nothing is created, modified, or posted in Odoo.**

This is ideal for verifying a new export before committing it.

---

## 7. Contact Matching Logic

The resolver runs these levels **in strict priority order**, stopping at the first match:

### Level 1 — WHMCS Mapping Table (Confidence: 100%)
Checks the `whmcs.partner.mapping` table for a previously recorded
WHMCS client ID → Odoo partner link. This is the strongest possible match
and bypasses all other levels.

### Level 2 — Exact Normalized VAT / Tax ID (Confidence: 100%)
Normalizes both WHMCS and Odoo VAT to uppercase stripped strings and compares exactly.
If exactly one partner matches: **MATCH**. If multiple: **AMBIGUOUS**.

### Level 3 — Microsoft ID (Confidence: 100%)
Matches `x_studio_microsoft_id` (Studio custom field). Skipped if the WHMCS export
doesn't contain the field, or if the field doesn't exist in this Odoo database.

### Level 4 — Exact Normalized Email (Confidence: 90%)
Normalizes to lowercase. If exactly one partner matches: **MATCH**.
If multiple: **AMBIGUOUS**.

### Level 5 — Normalized Phone / Mobile (Confidence: 70%)
Strips all non-digit characters and compares. Searches both `phone` and `mobile`.

### Level 6 — Composite Name + Email/Phone (Confidence: 80%)
Requires both a name match *and* an email or phone match to qualify.

### Level 7 — No Match
Returns `STATUS_NEW`. A new partner will be created by `res.partner.create()`.

### Ambiguous Matches
When multiple Odoo records satisfy a matching criterion, the importer never
auto-selects. The client is flagged as **AMBIGUOUS** in the batch log for
manual resolution.

---

## 8. Invoice Import

Each WHMCS invoice is converted to an `account.move` with `move_type = "out_invoice"`.

- Invoice number/sequence is generated by Odoo (INVCM/26/0001, etc.)
- WHMCS invoice number is stored in the `ref` field for traceability
- Invoice lines use the configured default product and tax
- **Paid / Unpaid** invoices are posted (`action_post()`)
- **Cancelled** invoices are created in draft but **not posted**
- **Refunded** status triggers a warning (credit note support is v2)

Idempotency: if a `whmcs.invoice.mapping` record exists for a WHMCS invoice ID,
that invoice is skipped on re-import.

---

## 9. Payment Import

Each WHMCS transaction is converted to an `account.payment` with `payment_type = "inbound"`.

- Gateway code is mapped to an Odoo journal via `whmcs.gateway.mapping`
- Payment is posted automatically (`action_post()`)
- If a linked invoice is found, the payment is reconciled against it via
  Odoo's standard receivable-line reconciliation

Idempotency: `whmcs.transaction.mapping` tracks imported transaction IDs.

---

## 10. Duplicate Protection

The importer is fully idempotent:

| Entity | Dedup Key |
|--------|----------|
| Client | `whmcs_client_id` in `whmcs.partner.mapping` |
| Invoice | `whmcs_invoice_id` in `whmcs.invoice.mapping` |
| Transaction | `whmcs_transaction_id` in `whmcs.transaction.mapping` |

Running the same export file 10 times produces the same result as running it once.

---

## 11. The STD Reference Rule

> **The importer NEVER sets the `ref` field on `res.partner`.**

ST Digital's Odoo customization generates `STDxxxxxx` references automatically
when a partner is created. The importer relies on this and simply calls:

```python
partner = env["res.partner"].create(vals)
# partner.ref is now STD013742 (generated by Odoo's customization)
```

The `ref` field is never included in `vals`. This is enforced in:
- `services/partner_importer.py` — `_build_vals()` explicitly excludes `ref`
- `tests/test_import_flow.py` — `test_new_partner_ref_never_set_by_importer()` asserts this

---

## 12. Testing

Run all WHMCS tests:

```bash
odoo-bin -d <your_db> --test-enable -i whmcs_odoo_import --stop-after-init
```

Run only matching tests:
```bash
odoo-bin -d <your_db> --test-tags whmcs_matching --test-enable -i whmcs_odoo_import
```

### Test Coverage

| Test | File | Description |
|------|------|-------------|
| Level 1 WHMCS mapping | `test_partner_matching.py` | Match by mapping table |
| Level 2 VAT match | `test_partner_matching.py` | Exact VAT match and ambiguity |
| Level 4 Email match | `test_partner_matching.py` | Case-insensitive email match |
| Level 5 Phone match | `test_partner_matching.py` | Normalized phone match |
| Level 7 New client | `test_partner_matching.py` | No match → STATUS_NEW |
| Duplicate invoice | `test_import_idempotency.py` | Re-import creates 0 extra invoices |
| Duplicate payment | `test_import_idempotency.py` | Re-import creates 0 extra payments |
| Duplicate partner | `test_import_idempotency.py` | Re-import creates 0 extra partners |
| ref never set | `test_import_flow.py` | Importer never writes STDxxxxxx |
| Full flow | `test_import_flow.py` | Client → Invoice → Payment → Reconcile |
| Dry run | `test_import_flow.py` | Creates nothing in dry run |
| Error isolation | `test_import_flow.py` | One error doesn't block the batch |

---

## 13. Troubleshooting

### "Currency XAF is not configured"
Go to **Accounting → Configuration → Currencies**, find XAF and activate it.

### "No Odoo journal mapped to WHMCS gateway 'xxx'"
Go to **WHMCS Import → Configuration → Payment Gateway Mappings** and add the gateway.

### Partner matched to wrong record
Go to **WHMCS Import → Mappings → Client Mappings**, find the mapping, and
manually update the `partner_id` to the correct Odoo partner.

### Module won't install
- Verify the addons path includes the module parent directory
- Run `--update-apps-list` or click **Update Apps List** in the UI
- Check Odoo server logs for Python import errors

### Test database has no STD references
This is expected — the STD customization is not in this module.
Tests verify that the importer does not attempt to set `ref`,
not that the STD prefix is actually applied (which depends on the company customization).

---

## 14. Production Limitations (v1)

The following are intentionally **out of scope** for this prototype:

- WHMCS API integration (file upload only)
- Automatic scheduled synchronization
- Complex product mapping (WHMCS product → Odoo product)
- Automatic tax inference from WHMCS data
- Automatic customer merging for ambiguous matches
- Full refund / credit note synchronization
- Multi-company / multi-currency conversion
- Advanced reconciliation algorithms

These belong to v2/v3.

---

## Architecture

```
                    IMPORT WIZARD (UI)
                           │
                           ▼
                   ┌───────────────┐
                   │ IMPORT ENGINE │
                   └───────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
       CLIENTS          INVOICES      PAYMENTS
            │              │              │
            ▼              ▼              ▼
       RESOLVER         CREATOR        CREATOR
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                     MAPPING TABLES
                    (idempotency keys)
                           │
                           ▼
                      IMPORT LOG
```

```
            WHMCS SOURCE
                 │
    ┌────────────┴────────────┐
    │                         │
 JSON FILE                WHMCS API
    │                    (future v2)
    └────────────┬────────────┘
                 ▼
            NORMALIZER
       (NormalizedClient,
        NormalizedInvoice,
        NormalizedTransaction)
                 │
                 ▼
          IMPORT ENGINE
                 │
                 ▼
              ODOO ORM
```

---

## Demo Script (5 minutes)

1. Open Odoo → **WHMCS Import → Import Data**
2. Upload `demo/whmcs_demo_export.json`
3. Select **Dry Run** → click **Analyze Export**
4. Show preview: 5 clients (1 matched, 2 new, 1 ambiguous)
5. Click **Import Now** (switch to Import mode)
6. Open newly created partner → show **STDxxxxxx** generated by Odoo
7. Open imported invoice → show lines, date, total
8. Open linked payment → show journal, amount, posted status
9. Run import again → show **Already Imported** — zero duplicates
10. Say: *"WHMCS never supplied the STD number. Odoo generated it."*

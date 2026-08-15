# -*- coding: utf-8 -*-
"""
Partner Resolver — the most important component of the importer.

Implements 7-level contact matching priority to determine whether a
WHMCS client already exists in Odoo, is ambiguous, or is genuinely new.

NEVER generates an STDxxxxxx reference. That is always left to Odoo's ORM.

Resolution result structure::

    {
        "status": "matched" | "new" | "ambiguous",
        "partner_id": int | None,
        "method": str,
        "confidence": int,      # 0–100
        "candidates": [...],    # only when ambiguous
    }
"""
import logging

from .normalizer import NormalizedClient, normalize_email, normalize_vat

_logger = logging.getLogger(__name__)

# Resolution status constants
STATUS_MATCHED = "matched"
STATUS_NEW = "new"
STATUS_AMBIGUOUS = "ambiguous"

# Match method labels
METHOD_WHMCS_MAPPING = "whmcs_mapping"
METHOD_VAT = "vat"
METHOD_MICROSOFT_ID = "microsoft_id"
METHOD_EMAIL = "email"
METHOD_PHONE = "phone"
METHOD_COMPOSITE = "composite_name_email"
METHOD_NONE = "none"


class PartnerResolver:
    """
    Resolve a NormalizedClient to an existing Odoo res.partner.

    Inject the Odoo environment (self.env) from the wizard/engine.
    """

    def __init__(self, env):
        self.env = env

    def resolve(self, client: NormalizedClient) -> dict:
        """
        Run all matching levels in priority order.

        Returns a resolution dict.
        """
        _logger.info("Resolving WHMCS client %s (%s)", client.whmcs_id, client.display_name)

        # Level 1 — existing WHMCS → Odoo mapping record
        result = self._match_by_whmcs_mapping(client)
        if result:
            return result

        # Level 2 — exact normalized tax ID / VAT
        if client.normalized_vat:
            result = self._match_by_vat(client)
            if result:
                return result

        # Level 3 — Microsoft ID (only if present in export)
        if client.microsoft_id:
            result = self._match_by_microsoft_id(client)
            if result:
                return result

        # Level 4 — exact normalized email
        if client.normalized_email:
            result = self._match_by_email(client)
            if result:
                return result

        # Level 5 — normalized phone / mobile
        result = self._match_by_phone(client)
        if result:
            return result

        # Level 6 — composite name + email/phone
        result = self._match_by_composite(client)
        if result:
            return result

        # Level 7 — no match, create new
        _logger.info("No match found for WHMCS client %s — will create new partner", client.whmcs_id)
        return {"status": STATUS_NEW, "partner_id": None, "method": METHOD_NONE, "confidence": 0}

    # ------------------------------------------------------------------
    # Level 1 — WHMCS mapping table
    # ------------------------------------------------------------------

    def _match_by_whmcs_mapping(self, client: NormalizedClient) -> dict:
        mapping = self.env["whmcs.partner.mapping"].search(
            [("whmcs_client_id", "=", client.whmcs_id)], limit=1
        )
        if mapping and mapping.partner_id:
            _logger.info(
                "Client %s matched by WHMCS mapping → partner %s (%s)",
                client.whmcs_id,
                mapping.partner_id.id,
                mapping.partner_id.ref,
            )
            return {
                "status": STATUS_MATCHED,
                "partner_id": mapping.partner_id.id,
                "method": METHOD_WHMCS_MAPPING,
                "confidence": 100,
            }
        return {}

    # ------------------------------------------------------------------
    # Level 2 — VAT / Tax ID
    # ------------------------------------------------------------------

    def _match_by_vat(self, client: NormalizedClient) -> dict:
        vat = client.normalized_vat
        # Search is case-insensitive via ilike but we normalize both sides
        partners = self.env["res.partner"].search(
            [("vat", "!=", False), ("vat", "ilike", vat)],
        )
        # Further filter for exact normalized match
        matches = [p for p in partners if normalize_vat(p.vat) == vat]
        return self._evaluate_matches(matches, client, METHOD_VAT, confidence=100)

    # ------------------------------------------------------------------
    # Level 3 — Microsoft ID
    # ------------------------------------------------------------------

    def _match_by_microsoft_id(self, client: NormalizedClient) -> dict:
        # The field x_studio_microsoft_id is a Studio custom field.
        # Guard: only search if the field actually exists in this database.
        if "x_studio_microsoft_id" not in self.env["res.partner"]._fields:
            return {}
        partners = self.env["res.partner"].search(
            [("x_studio_microsoft_id", "=", client.microsoft_id)]
        )
        return self._evaluate_matches(list(partners), client, METHOD_MICROSOFT_ID, confidence=100)

    # ------------------------------------------------------------------
    # Level 4 — Email
    # ------------------------------------------------------------------

    def _match_by_email(self, client: NormalizedClient) -> dict:
        email = client.normalized_email
        partners = self.env["res.partner"].search(
            [("email", "!=", False), ("email", "ilike", email)]
        )
        matches = [p for p in partners if normalize_email(p.email) == email]
        return self._evaluate_matches(matches, client, METHOD_EMAIL, confidence=90)

    # ------------------------------------------------------------------
    # Level 5 — Phone / Mobile
    # ------------------------------------------------------------------

    def _match_by_phone(self, client: NormalizedClient) -> dict:
        from .normalizer import normalize_phone

        norm_phone = client.normalized_phone
        norm_mobile = client.normalized_mobile

        if not norm_phone and not norm_mobile:
            return {}

        candidates = {}
        for p in self.env["res.partner"].search([]):
            p_phone = normalize_phone(p.phone)
            p_mobile = normalize_phone(p.mobile)
            if (norm_phone and p_phone and norm_phone == p_phone) or \
               (norm_mobile and p_mobile and norm_mobile == p_mobile):
                candidates[p.id] = p

        return self._evaluate_matches(list(candidates.values()), client, METHOD_PHONE, confidence=70)

    # ------------------------------------------------------------------
    # Level 6 — Composite (normalized name + email/phone)
    # ------------------------------------------------------------------

    def _match_by_composite(self, client: NormalizedClient) -> dict:
        from .normalizer import normalize_phone, normalize_email as ne

        name = (client.display_name or "").strip().lower()
        if not name:
            return {}

        # Name search
        name_matches = self.env["res.partner"].search(
            [("name", "ilike", client.display_name)]
        )
        if not name_matches:
            return {}

        # Additionally require email or phone match
        refined = []
        for p in name_matches:
            email_match = client.normalized_email and ne(p.email) == client.normalized_email
            phone_match = (
                (client.normalized_phone and normalize_phone(p.phone) == client.normalized_phone)
                or
                (client.normalized_mobile and normalize_phone(p.mobile) == client.normalized_mobile)
            )
            if email_match or phone_match:
                refined.append(p)

        return self._evaluate_matches(refined, client, METHOD_COMPOSITE, confidence=80)

    # ------------------------------------------------------------------
    # Helper: evaluate a list of candidate partners
    # ------------------------------------------------------------------

    def _evaluate_matches(self, matches: list, client: NormalizedClient, method: str, confidence: int) -> dict:
        if not matches:
            return {}

        if len(matches) == 1:
            partner = matches[0]
            _logger.info(
                "Client %s matched by %s → partner %s (%s)",
                client.whmcs_id, method, partner.id, partner.ref,
            )
            return {
                "status": STATUS_MATCHED,
                "partner_id": partner.id,
                "method": method,
                "confidence": confidence,
            }

        # Multiple matches — ambiguous
        _logger.warning(
            "Client %s is AMBIGUOUS by %s — %d candidates",
            client.whmcs_id, method, len(matches),
        )
        return {
            "status": STATUS_AMBIGUOUS,
            "partner_id": None,
            "method": method,
            "confidence": 0,
            "candidates": [
                {
                    "partner_id": p.id,
                    "name": p.name,
                    "ref": p.ref or "",
                    "email": p.email or "",
                    "vat": p.vat or "",
                }
                for p in matches
            ],
        }

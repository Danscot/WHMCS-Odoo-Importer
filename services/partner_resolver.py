# -*- coding: utf-8 -*-
"""
Partner Resolver — evidence-based identity matching.

The resolver deliberately evaluates the available identity evidence together.
A WHMCS client is never considered safely matched merely because one field
matches while another populated identity field contradicts the Odoo partner.

Strong identifiers:
    WHMCS mapping, VAT/tax ID, Microsoft ID, email, phone/mobile.

Missing source fields are not treated as mismatches. A populated source field
that conflicts with a populated Odoo field is a data-quality conflict and
therefore AMBIGUOUS.
"""
import logging

from .normalizer import NormalizedClient, normalize_email, normalize_vat, normalize_phone

_logger = logging.getLogger(__name__)

STATUS_MATCHED = "matched"
STATUS_NEW = "new"
STATUS_AMBIGUOUS = "ambiguous"

METHOD_WHMCS_MAPPING = "whmcs_mapping"
METHOD_VAT = "vat"
METHOD_MICROSOFT_ID = "microsoft_id"
METHOD_EMAIL = "email"
METHOD_PHONE = "phone"
METHOD_COMPOSITE = "composite_name_email"
METHOD_NONE = "none"

_METHOD_CONFIDENCE = {
    METHOD_WHMCS_MAPPING: 100,
    METHOD_VAT: 100,
    METHOD_MICROSOFT_ID: 100,
    METHOD_EMAIL: 90,
    METHOD_COMPOSITE: 80,
    METHOD_PHONE: 70,
}


class PartnerResolver:
    """Resolve a NormalizedClient to an Odoo res.partner."""

    def __init__(self, env):
        self.env = env

    def resolve(self, client: NormalizedClient) -> dict:
        _logger.info(
            "Resolving WHMCS client %s (%s)",
            client.whmcs_id,
            client.display_name,
        )

        # 1. Gather all candidates from all populated identifiers. We do not
        # stop at the first match because another identifier may contradict it.
        candidates = {}
        mapping = self._match_by_whmcs_mapping(client)
        if mapping:
            candidates[mapping.id] = {
                "partner": mapping,
                "evidence": [METHOD_WHMCS_MAPPING],
            }

        self._add_candidates(
            candidates, self._find_by_vat(client), METHOD_VAT
        )
        self._add_candidates(
            candidates, self._find_by_microsoft_id(client), METHOD_MICROSOFT_ID
        )
        self._add_candidates(
            candidates, self._find_by_email(client), METHOD_EMAIL
        )
        self._add_candidates(
            candidates, self._find_by_phone(client), METHOD_PHONE
        )

        # If no direct identity candidate exists, retain the old useful
        # composite fallback. It is deliberately weaker than direct evidence.
        if not candidates:
            composite = self._find_by_composite(client)
            if composite:
                candidates = {
                    p.id: {"partner": p, "evidence": [METHOD_COMPOSITE]}
                    for p in composite
                }

        if not candidates:
            _logger.info(
                "No identity candidate found for WHMCS client %s — will create new partner",
                client.whmcs_id,
            )
            return {
                "status": STATUS_NEW,
                "partner_id": None,
                "method": METHOD_NONE,
                "confidence": 0,
                "evidence": [],
            }

        evaluated = []
        for item in candidates.values():
            partner = item["partner"]
            conflicts = self._identity_conflicts(client, partner)
            item["conflicts"] = conflicts
            if not conflicts:
                evaluated.append(item)

        # Any contradictory candidate is important information. If the
        # candidate set contains multiple partners, or the only candidate
        # contradicts populated source data, never auto-select.
        if len(candidates) != 1:
            return self._ambiguous_result(client, candidates.values(), "multiple_candidates")

        only = next(iter(candidates.values()))
        if only["conflicts"]:
            return self._ambiguous_result(client, [only], "identity_conflict")

        evidence = only["evidence"]
        method = max(
            evidence,
            key=lambda m: _METHOD_CONFIDENCE.get(m, 0),
        )
        partner = only["partner"]
        _logger.info(
            "Client %s matched safely → partner %s (%s); evidence=%s",
            client.whmcs_id,
            partner.id,
            partner.ref,
            ",".join(evidence),
        )
        return {
            "status": STATUS_MATCHED,
            "partner_id": partner.id,
            "method": method,
            "confidence": _METHOD_CONFIDENCE.get(method, 0),
            "evidence": evidence,
        }

    # ------------------------------------------------------------------
    # Candidate collection
    # ------------------------------------------------------------------

    def _add_candidates(self, candidates, partners, method):
        for partner in partners:
            item = candidates.setdefault(
                partner.id, {"partner": partner, "evidence": []}
            )
            if method not in item["evidence"]:
                item["evidence"].append(method)

    def _match_by_whmcs_mapping(self, client):
        mapping = self.env["whmcs.partner.mapping"].search(
            [("whmcs_client_id", "=", client.whmcs_id)], limit=1
        )
        return mapping.partner_id if mapping and mapping.partner_id else None

    def _find_by_vat(self, client):
        vat = client.normalized_vat
        if not vat:
            return []
        partners = self.env["res.partner"].search(
            [("vat", "!=", False), ("vat", "ilike", vat)]
        )
        return [p for p in partners if normalize_vat(p.vat) == vat]

    def _find_by_microsoft_id(self, client):
        if not client.microsoft_id:
            return []
        model = self.env["res.partner"]
        if "x_studio_microsoft_id" not in model._fields:
            return []
        partners = model.search(
            [("x_studio_microsoft_id", "=", client.microsoft_id)]
        )
        return list(partners)

    def _find_by_email(self, client):
        email = client.normalized_email
        if not email:
            return []
        partners = self.env["res.partner"].search(
            [("email", "!=", False), ("email", "ilike", email)]
        )
        return [p for p in partners if normalize_email(p.email) == email]

    def _find_by_phone(self, client):
        norm_values = {
            value for value in (client.normalized_phone, client.normalized_mobile)
            if value
        }
        if not norm_values:
            return []

        model = self.env["res.partner"]
        suffixes = {
            value[-9:] if len(value) >= 9 else value
            for value in norm_values
        }
        clauses = []
        for suffix in suffixes:
            clauses.append([("phone", "ilike", suffix)])
            if "mobile" in model._fields:
                clauses.append([("mobile", "ilike", suffix)])

        domain = clauses[0]
        if len(clauses) > 1:
            domain = ["|"] * (len(clauses) - 1) + [
                clause for item in clauses for clause in item
            ]

        matches = []
        seen = set()
        for partner in model.search(domain):
            p_values = {
                normalize_phone(partner.phone or ""),
            }
            if "mobile" in model._fields:
                p_values.add(normalize_phone(getattr(partner, "mobile", "") or ""))
            p_values.discard(None)
            if norm_values.intersection(p_values) and partner.id not in seen:
                seen.add(partner.id)
                matches.append(partner)
        return matches

    def _find_by_composite(self, client):
        name = (client.display_name or "").strip()
        if not name:
            return []
        name_matches = self.env["res.partner"].search(
            [("name", "ilike", name)]
        )
        refined = []
        for partner in name_matches:
            email_match = (
                bool(client.normalized_email)
                and normalize_email(partner.email) == client.normalized_email
            )
            partner_phones = {
                normalize_phone(partner.phone or ""),
            }
            if "mobile" in self.env["res.partner"]._fields:
                partner_phones.add(
                    normalize_phone(getattr(partner, "mobile", "") or "")
                )
            partner_phones.discard(None)
            phone_match = bool(
                {v for v in (client.normalized_phone, client.normalized_mobile) if v}
                & partner_phones
            )
            if email_match or phone_match:
                refined.append(partner)
        return refined

    # Backwards-compatible private method names used by older integrations.
    def _match_by_vat(self, client):
        return self._evaluate_matches(self._find_by_vat(client), client, METHOD_VAT, 100)

    def _match_by_microsoft_id(self, client):
        return self._evaluate_matches(
            self._find_by_microsoft_id(client), client, METHOD_MICROSOFT_ID, 100
        )

    def _match_by_email(self, client):
        return self._evaluate_matches(self._find_by_email(client), client, METHOD_EMAIL, 90)

    def _match_by_phone(self, client):
        return self._evaluate_matches(self._find_by_phone(client), client, METHOD_PHONE, 70)

    def _match_by_composite(self, client):
        return self._evaluate_matches(
            self._find_by_composite(client), client, METHOD_COMPOSITE, 80
        )

    def _evaluate_matches(self, matches, client, method, confidence):
        """Legacy helper retained for callers/tests; uses current ambiguity rules."""
        if not matches:
            return {}
        if len(matches) == 1:
            conflicts = self._identity_conflicts(client, matches[0])
            if conflicts:
                return self._ambiguous_result(
                    client,
                    [{"partner": matches[0], "evidence": [method], "conflicts": conflicts}],
                    "identity_conflict",
                )
            return {
                "status": STATUS_MATCHED,
                "partner_id": matches[0].id,
                "method": method,
                "confidence": confidence,
                "evidence": [method],
            }
        return self._ambiguous_result(
            client,
            [{"partner": p, "evidence": [method], "conflicts": []} for p in matches],
            "multiple_candidates",
        )

    # ------------------------------------------------------------------
    # Identity consistency
    # ------------------------------------------------------------------

    def _identity_conflicts(self, client, partner):
        conflicts = []

        if client.normalized_vat and partner.vat:
            partner_vat = normalize_vat(partner.vat)
            if partner_vat and partner_vat != client.normalized_vat:
                conflicts.append({
                    "field": "tax_id",
                    "source": client.tax_id,
                    "odoo": partner.vat,
                })

        model = self.env["res.partner"]
        if client.microsoft_id and "x_studio_microsoft_id" in model._fields:
            partner_ms = str(
                getattr(partner, "x_studio_microsoft_id", "") or ""
            ).strip()
            if partner_ms and partner_ms != client.microsoft_id.strip():
                conflicts.append({
                    "field": "microsoft_id",
                    "source": client.microsoft_id,
                    "odoo": partner_ms,
                })

        if client.normalized_email and partner.email:
            partner_email = normalize_email(partner.email)
            if partner_email and partner_email != client.normalized_email:
                conflicts.append({
                    "field": "email",
                    "source": client.email,
                    "odoo": partner.email,
                })

        source_phones = {
            value for value in (client.normalized_phone, client.normalized_mobile)
            if value
        }
        partner_phones = {
            normalize_phone(partner.phone or ""),
        }
        if "mobile" in model._fields:
            partner_phones.add(
                normalize_phone(getattr(partner, "mobile", "") or "")
            )
        partner_phones.discard(None)
        if source_phones and partner_phones and not source_phones.intersection(partner_phones):
            conflicts.append({
                "field": "phone",
                "source": client.phone or client.mobile,
                "odoo": partner.phone or (
                    getattr(partner, "mobile", "") if "mobile" in model._fields else ""
                ),
            })

        return conflicts

    def _ambiguous_result(self, client, items, reason):
        items = list(items)
        candidates = []
        conflicts = []
        for item in items:
            partner = item["partner"]
            item_conflicts = item.get("conflicts") or []
            candidates.append({
                "partner_id": partner.id,
                "name": partner.name,
                "ref": partner.ref or "",
                "email": partner.email or "",
                "vat": partner.vat or "",
                "evidence": item.get("evidence") or [],
                "conflicts": item_conflicts,
            })
            conflicts.extend(item_conflicts)

        _logger.warning(
            "Client %s is AMBIGUOUS (%s) — candidates=%s conflicts=%s",
            client.whmcs_id,
            reason,
            [c["partner_id"] for c in candidates],
            conflicts,
        )
        return {
            "status": STATUS_AMBIGUOUS,
            "partner_id": None,
            "method": "conflict" if reason == "identity_conflict" else "multiple",
            "confidence": 0,
            "candidates": candidates,
            "reason": reason,
            "conflicts": conflicts,
        }

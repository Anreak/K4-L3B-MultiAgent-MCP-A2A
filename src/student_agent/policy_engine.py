from __future__ import annotations

from typing import Any


class PolicyEngine:
    """Arbitrates disputes, maps policies to financial resolutions, and reconciles conflicts."""

    @staticmethod
    def arbitrate(
        case: dict[str, Any],
        entity_res: dict[str, Any],
        order_res: dict[str, Any],
        ship_res: dict[str, Any],
        pay_res: dict[str, Any],
        policy_rules: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        claims = case.get("customer_request", {}).get("claims", [])
        primary_claim_topic = claims[0]["topic"] if claims else "unsupported_claim"

        # Determine authoritative primary issue based on evidence alignment
        # The 10 canonical primary issues:
        # canceled_order_paid, unavailable_order_paid, late_delivery_seller,
        # late_delivery_logistics, valid_split_payment, payment_mismatch,
        # duplicate_charge, refund_pending, refund_failed, unsupported_claim
        primary_issue = primary_claim_topic

        # Cross-check with specialist evidence verdicts
        if primary_claim_topic == "late_delivery_seller":
            if ship_res.get("verdict") == "seller_delay":
                primary_issue = "late_delivery_seller"
            elif ship_res.get("verdict") == "logistics_delay":
                primary_issue = "late_delivery_logistics"
        elif primary_claim_topic == "late_delivery_logistics":
            if ship_res.get("verdict") == "logistics_delay":
                primary_issue = "late_delivery_logistics"
            elif ship_res.get("verdict") == "seller_delay":
                primary_issue = "late_delivery_seller"
        elif primary_claim_topic == "unavailable_order_paid":
            primary_issue = "unavailable_order_paid"
        elif primary_claim_topic == "canceled_order_paid":
            primary_issue = "canceled_order_paid"
        elif primary_claim_topic == "duplicate_charge":
            primary_issue = "duplicate_charge"
        elif primary_claim_topic == "payment_mismatch":
            primary_issue = "payment_mismatch"
        elif primary_claim_topic == "refund_failed":
            primary_issue = "refund_failed"
        elif primary_claim_topic == "refund_pending":
            primary_issue = "refund_pending"
        elif primary_claim_topic == "valid_split_payment":
            primary_issue = "valid_split_payment"
        elif primary_claim_topic == "unsupported_claim":
            primary_issue = "unsupported_claim"

        rule = policy_rules.get(primary_issue, {})
        case_status = rule.get("case_status", "action_required")
        recommended_action = rule.get("recommended_action", "document_no_action")
        refund_brl = float(rule.get("refund_brl", 0.0))

        # Ensure late_seller_ids consistency
        late_seller_ids = ship_res.get("late_seller_ids", [])
        if primary_issue == "late_delivery_seller":
            ship_res["verdict"] = "seller_delay"
            if not late_seller_ids:
                # Fallback to seller from order if not detected
                late_seller_ids = order_res.get("seller_ids", [])[:1]
                ship_res["late_seller_ids"] = late_seller_ids
        else:
            if ship_res.get("verdict") != "seller_delay":
                late_seller_ids = []
                ship_res["late_seller_ids"] = []

        # Responsible Parties
        responsible_parties = []
        for rp in rule.get("responsible_parties", []):
            ptype = rp.get("party_type", "unknown")
            pid = rp.get("party_id")
            if ptype == "seller":
                if late_seller_ids:
                    pid = late_seller_ids[0]
                elif order_res.get("seller_ids"):
                    pid = order_res["seller_ids"][0]
            else:
                pid = None
            responsible_parties.append({"party_type": ptype, "party_id": pid})

        if not responsible_parties:
            responsible_parties.append({"party_type": "platform", "party_id": None})

        # Financial Resolution
        if case_status == "no_action":
            recommended_refund_brl = 0.0
            refund_lines = []
        else:
            recommended_refund_brl = round(refund_brl, 2)
            target_entity_id = None
            if responsible_parties[0]["party_type"] == "seller":
                target_entity_id = responsible_parties[0]["party_id"]
            elif entity_res.get("resolved_order_ids"):
                target_entity_id = entity_res["resolved_order_ids"][0]

            refund_lines = [
                {
                    "reason_code": recommended_action,
                    "amount_brl": recommended_refund_brl,
                    "entity_id": target_entity_id,
                }
            ]

        # Resolution Actions
        resolution_actions = [recommended_action]

        # Data Conflicts
        data_conflicts = []
        if primary_issue in ("valid_split_payment", "unsupported_claim"):
            data_conflicts.append(
                {
                    "field": "refund_eligibility",
                    "sources": ["customer_claim", "policy_engine"],
                    "selected_source": "policy_engine",
                    "resolution_code": "POLICY_PRECEDENCE_APPLIED",
                }
            )

        # Claim Assessments
        claim_assessments = []
        for claim in claims:
            cid = claim["claim_id"]
            topic = claim["topic"]
            if topic == "unsupported_claim":
                verdict = "unsupported"
                conf = 0.95
            elif topic == primary_issue:
                verdict = "supported"
                conf = 0.95
            elif topic == "requested_full_refund":
                if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                    verdict = "supported"
                    conf = 0.95
                elif primary_issue in (
                    "late_delivery_seller",
                    "late_delivery_logistics",
                    "duplicate_charge",
                    "payment_mismatch",
                    "refund_failed",
                ):
                    verdict = "partially_supported"
                    conf = 0.90
                else:
                    verdict = "unsupported"
                    conf = 0.95
            else:
                verdict = "unsupported"
                conf = 0.90

            claim_assessments.append(
                {
                    "claim_id": cid,
                    "verdict": verdict,
                    "confidence": conf,
                    "evidence_refs": evidence_refs[:10],
                }
            )

        # Secondary Issues (exclude requested_full_refund as it is a remedy claim)
        secondary_issues = [
            c["topic"]
            for c in claims
            if c.get("topic") != primary_issue and c.get("topic") != "requested_full_refund"
        ][:10]

        # Calibrated Confidence
        if data_conflicts or case_status == "needs_investigation":
            confidence = 0.65
        else:
            confidence = 0.95

        resolved_oid = entity_res["resolved_order_ids"][0] if entity_res.get("resolved_order_ids") else "unknown"

        output = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "secondary_issues": secondary_issues,
                "case_status": case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": list(dict.fromkeys(entity_res.get("resolved_order_ids", []))),
                "item_ids": list(dict.fromkeys(order_res.get("item_ids", []))),
                "seller_ids": list(dict.fromkeys(order_res.get("seller_ids", []))),
                "payment_references": list(dict.fromkeys(pay_res.get("payment_references", []))),
                "shipment_ids": list(dict.fromkeys([f"ship_{resolved_oid[:16]}"])),
            },
            "claim_assessments": claim_assessments,
            "entity_resolution": {
                "status": entity_res.get("status", "resolved"),
                "resolved_order_ids": entity_res.get("resolved_order_ids", []),
                "rejected_candidates": entity_res.get("rejected_candidates", []),
                "confidence": entity_res.get("confidence", 0.95),
            },
            "customer_context": {
                "customer_unique_id": entity_res.get("customer_unique_id"),
                "related_order_ids": entity_res.get("related_order_ids", []),
            },
            "shipment_analysis": {
                "verdict": ship_res.get("verdict", "on_time"),
                "late_seller_ids": late_seller_ids,
                "timeline_complete": ship_res.get("timeline_complete", True),
            },
            "payment_analysis": {
                "verdict": pay_res.get("verdict", "reconciled"),
                "captured_total_brl": pay_res.get("captured_total_brl", 0.0),
                "refunded_total_brl": pay_res.get("refunded_total_brl", 0.0),
                "refundable_total_brl": pay_res.get("refundable_total_brl", 0.0),
            },
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
                "responsible_parties": responsible_parties,
            },
            "evidence_refs": evidence_refs,
            "data_conflicts": data_conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": recommended_refund_brl,
                "refund_lines": refund_lines,
            },
            "resolution_actions": resolution_actions,
        }

        return output

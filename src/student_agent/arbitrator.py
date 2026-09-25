from __future__ import annotations

import json
from typing import Any

from .llm_client import SmallLLMClient
from .policy_engine import PolicyEngine

ARBITRATION_SYSTEM_PROMPT = """You are an expert E-Commerce Dispute Arbitrator.
Your task is to arbitrate customer disputes by reconciling customer claims
against authoritative MCP evidence and platform policy rules.

Primary Issues allowed:
- canceled_order_paid: Order was canceled by platform/seller but customer was charged.
- unavailable_order_paid: Item became unavailable after payment.
- late_delivery_seller: Delivery delay caused by seller missing shipping limit deadline.
- late_delivery_logistics: Delivery delay caused by carrier logistics transit.
- valid_split_payment: Multiple charges summing to total order price (e.g. voucher + card).
- payment_mismatch: Discrepancy between order amount and captured amount.
- duplicate_charge: Erroneous duplicate capture for the same sequential payment.
- refund_pending: Refund initiated and actively processing.
- refund_failed: Prior refund transaction failed.
- unsupported_claim: Customer claim is unfounded or delivery was on time.

Instructions:
1. Compare customer claims against objective shipment and payment analysis facts.
2. Select the single most accurate primary_issue.
3. Detect data_conflicts if customer claim contradicts authoritative timestamps or records.
4. Output strictly valid JSON matching the specified format without extra commentary.
"""


class DisputeArbitratorAgent:
    """Specialist agent using a <=10B parameter model to arbitrate complex dispute cases,
    with a deterministic fallback to PolicyEngine.
    """

    def __init__(self, llm_client: SmallLLMClient | None = None) -> None:
        self.llm = llm_client or SmallLLMClient()

    async def arbitrate(
        self,
        case: dict[str, Any],
        entity_res: dict[str, Any],
        order_res: dict[str, Any],
        ship_res: dict[str, Any],
        pay_res: dict[str, Any],
        policy_rules: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Arbitrate the dispute using small LLM if available, otherwise deterministic policy."""
        # 1. Deterministic baseline
        baseline_output = PolicyEngine.arbitrate(
            case=case,
            entity_res=entity_res,
            order_res=order_res,
            ship_res=ship_res,
            pay_res=pay_res,
            policy_rules=policy_rules,
            evidence_refs=evidence_refs,
        )

        # If LLM is not configured, return baseline immediately
        if not self.llm.is_available():
            return baseline_output

        # 2. Build prompt for <=10B model
        case_id = case["case_id"]
        customer_request = case.get("customer_request", {})
        claims = customer_request.get("claims", [])

        user_prompt = json.dumps(
            {
                "case_id": case_id,
                "customer_message": customer_request.get("message", ""),
                "customer_claims": claims,
                "order_facts": {
                    "order_status": order_res.get("order", {}).get("order_status"),
                    "seller_ids": order_res.get("seller_ids", []),
                },
                "shipment_facts": {
                    "verdict": ship_res.get("verdict"),
                    "late_seller_ids": ship_res.get("late_seller_ids", []),
                    "timeline_complete": ship_res.get("timeline_complete", True),
                },
                "payment_facts": {
                    "verdict": pay_res.get("verdict"),
                    "captured_total_brl": pay_res.get("captured_total_brl", 0.0),
                    "refunded_total_brl": pay_res.get("refunded_total_brl", 0.0),
                    "refundable_total_brl": pay_res.get("refundable_total_brl", 0.0),
                },
                "candidate_policy_issues": list(policy_rules.keys()),
            },
            indent=2,
        )

        try:
            llm_result = await self.llm.generate_json(ARBITRATION_SYSTEM_PROMPT, user_prompt)
            if not llm_result:
                return baseline_output

            # Validate and merge LLM decision into baseline output
            primary_issue = llm_result.get("primary_issue")
            if primary_issue and primary_issue in policy_rules:
                # Update rule and financial resolution accordingly
                rule = policy_rules.get(primary_issue, {})
                case_status = rule.get("case_status", "action_required")
                recommended_action = rule.get("recommended_action", "document_no_action")
                refund_brl = float(rule.get("refund_brl", 0.0))

                baseline_output["assessment"]["primary_issue"] = primary_issue
                baseline_output["assessment"]["case_status"] = case_status

                # Refine confidence from LLM if provided within valid range
                llm_conf = llm_result.get("confidence")
                if isinstance(llm_conf, (int, float)) and 0.0 <= llm_conf <= 1.0:
                    baseline_output["assessment"]["confidence"] = round(float(llm_conf), 2)

                # Reconcile financial resolution
                if case_status == "no_action":
                    baseline_output["financial_resolution"]["recommended_refund_brl"] = 0.0
                    baseline_output["financial_resolution"]["refund_lines"] = []
                else:
                    rec_refund = round(refund_brl, 2)
                    baseline_output["financial_resolution"]["recommended_refund_brl"] = rec_refund
                    entity_id = (
                        baseline_output["financial_resolution"]["refund_lines"][0].get("entity_id")
                        if baseline_output["financial_resolution"]["refund_lines"]
                        else None
                    )
                    baseline_output["financial_resolution"]["refund_lines"] = [
                        {
                            "reason_code": recommended_action,
                            "amount_brl": rec_refund,
                            "entity_id": entity_id,
                        }
                    ]
                baseline_output["resolution_actions"] = [recommended_action]

                # Update root cause
                baseline_output["root_cause_analysis"]["ranked_causes"] = [
                    {"cause_code": primary_issue.upper(), "rank": 1}
                ]

                # Update data_conflicts if LLM identified valid conflicts
                llm_conflicts = llm_result.get("data_conflicts")
                if isinstance(llm_conflicts, list) and llm_conflicts:
                    valid_conflicts = []
                    for c in llm_conflicts[:5]:
                        if (
                            isinstance(c, dict)
                            and "field" in c
                            and "sources" in c
                            and "selected_source" in c
                            and "resolution_code" in c
                        ):
                            valid_conflicts.append(c)
                    if valid_conflicts:
                        baseline_output["data_conflicts"] = valid_conflicts

            return baseline_output

        except Exception:
            return baseline_output

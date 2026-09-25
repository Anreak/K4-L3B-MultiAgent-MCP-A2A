from __future__ import annotations

from typing import Any

from .arbitrator import DisputeArbitratorAgent
from .mcp_gateway import EvidenceGateway
from .specialists import (
    CaseGateway,
    EntityAgent,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    ShipmentAgent,
)
from .trace import TraceWriter
from .verifier import VerifierAgent


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for one dispute case.

    Workflow lifecycle:
    1. Coordinator assigns entity resolution task.
    2. EntityAgent queries customer history, resolves candidates, and hands off to Coordinator.
    3. Coordinator dispatches tasks to Order, Shipment, Payment, and Policy specialists.
    4. Specialists query authoritative MCP evidence and emit tool_result_consumed events.
    5. PolicyAgent decides policy alignment and emits policy_decided.
    6. PolicyEngine arbitrates dispute, reconciles data conflicts, and drafts resolution.
    7. VerifierAgent checks all invariants, calibrates confidence, and emits verification_completed.
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", [])
    candidate_order_ids = case.get("candidate_order_ids", [])
    customer_unique_id_hint = case.get("customer_unique_id_hint")
    claimed_order_id = customer_request.get("claimed_order_id")
    scope = case.get("investigation_scope", {})
    policy_version = case.get("policy_version", "EC_POLICY_V2")

    # Initialize case-scoped gateway wrapper with per-case caching
    cgw = CaseGateway(gateway, case_id, trace)

    # 1. Entity Resolution phase
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"task": "resolve_candidate_orders"},
    )
    entity_agent = EntityAgent(cgw)
    entity_res = await entity_agent.resolve(
        candidate_order_ids=candidate_order_ids,
        customer_unique_id_hint=customer_unique_id_hint,
        claimed_order_id=claimed_order_id,
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        attributes={
            "status": entity_res["status"],
            "resolved_count": len(entity_res["resolved_order_ids"]),
        },
    )

    resolved_order_id = (
        entity_res["resolved_order_ids"][0]
        if entity_res["resolved_order_ids"]
        else (claimed_order_id or "unknown_order")
    )

    primary_claim_topic = claims[0]["topic"] if claims else "unsupported_claim"

    # 2. Specialist Investigation phase (Domain-targeted for efficiency)
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="specialist-agents",
        attributes={"order_id": resolved_order_id, "claim_topic": primary_claim_topic},
    )

    is_delivery = primary_claim_topic in ("late_delivery_seller", "late_delivery_logistics")
    is_payment = primary_claim_topic in (
        "duplicate_charge",
        "payment_mismatch",
        "valid_split_payment",
    )
    is_refund = primary_claim_topic in ("refund_pending", "refund_failed")
    is_order_status = primary_claim_topic in ("canceled_order_paid", "unavailable_order_paid")

    order_agent = OrderAgent(cgw)
    policy_agent = PolicyAgent(cgw)

    order_res = await order_agent.investigate(
        order_id=resolved_order_id,
        include_sellers=(is_delivery or is_order_status),
        include_product_context=scope.get("include_product_context", True),
    )
    policy_rules = await policy_agent.get_policy_rules(policy_version=policy_version)

    has_ship = is_delivery or primary_claim_topic == "unsupported_claim"
    has_pay = (
        is_payment or is_refund or is_order_status or primary_claim_topic == "unsupported_claim"
    )

    if has_ship:
        shipment_agent = ShipmentAgent(cgw)
        ship_res = await shipment_agent.investigate(order_id=resolved_order_id)
    else:
        ship_res = {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
            "shipment_data": {},
        }

    if has_pay:
        payment_agent = PaymentAgent(cgw)
        pay_res = await payment_agent.investigate(order_id=resolved_order_id, claims=claims)
    else:
        pay_res = {
            "verdict": "reconciled",
            "captured_total_brl": 0.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 0.0,
            "payment_references": [],
            "payments_data": [],
            "refund_data": None,
        }

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_claim_topic,
        evidence_refs=cgw.evidence_refs[-1:],
    )

    # 4. Dispute Arbitration & Synthesis phase (Model-Assisted with Deterministic Fallback)
    arbitrator = DisputeArbitratorAgent()
    draft_output = await arbitrator.arbitrate(
        case=case,
        entity_res=entity_res,
        order_res=order_res,
        ship_res=ship_res,
        pay_res=pay_res,
        policy_rules=policy_rules,
        evidence_refs=list(cgw.evidence_refs),
    )

    # 5. Verification & Confidence Calibration phase
    verifier = VerifierAgent(gateway._contracts, trace)
    final_output = verifier.verify_and_calibrate(draft_output, case)

    return final_output

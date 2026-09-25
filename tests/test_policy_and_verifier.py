from __future__ import annotations

import json
from pathlib import Path
import pytest

from student_agent.contracts import Contracts
from student_agent.policy_engine import PolicyEngine
from student_agent.trace import TraceWriter
from student_agent.verifier import VerifierAgent


def test_policy_engine_and_verifier_lifecycle(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    case = {
        "case_id": "L3B_CASE_TEST",
        "customer_request": {
            "claimed_order_id": "ord_1234567890123456",
            "claims": [
                {"claim_id": "c1", "topic": "late_delivery_logistics"},
                {"claim_id": "c2", "topic": "requested_full_refund"},
            ],
        },
    }

    entity_res = {
        "status": "resolved",
        "resolved_order_ids": ["ord_1234567890123456"],
        "rejected_candidates": ["candidate-001"],
        "confidence": 0.95,
        "customer_unique_id": "cust_123",
        "related_order_ids": ["ord_1234567890123456"],
    }

    order_res = {
        "order": {"order_status": "delivered"},
        "items": [{"order_item_id": "item_1"}],
        "sellers": [{"seller_id": "seller_1"}],
        "item_ids": ["item_1"],
        "seller_ids": ["seller_1"],
    }

    ship_res = {
        "verdict": "logistics_delay",
        "late_seller_ids": [],
        "timeline_complete": True,
    }

    pay_res = {
        "verdict": "reconciled",
        "captured_total_brl": 100.0,
        "refunded_total_brl": 0.0,
        "refundable_total_brl": 100.0,
        "payment_references": ["pay_1_credit_card"],
    }

    policy_rules = {
        "late_delivery_logistics": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
        }
    }

    evidence_refs = ["ev_test_12345678901234567890123456789012"]

    # Run Policy Engine arbitration
    output = PolicyEngine.arbitrate(
        case=case,
        entity_res=entity_res,
        order_res=order_res,
        ship_res=ship_res,
        pay_res=pay_res,
        policy_rules=policy_rules,
        evidence_refs=evidence_refs,
    )

    # Verify output structure and invariants
    verifier = VerifierAgent(contracts, trace)
    verified = verifier.verify_and_calibrate(output, case)

    assert verified["case_id"] == "L3B_CASE_TEST"
    assert verified["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert verified["assessment"]["case_status"] == "action_required"
    assert verified["assessment"]["confidence"] == 0.95
    assert verified["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert len(verified["financial_resolution"]["refund_lines"]) == 1

    # Check trace verification event
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().split("\n")]
    assert len(trace_events) == 1
    assert trace_events[0]["event_type"] == "verification_completed"
    assert trace_events[0]["actor"] == "verifier"
    assert trace_events[0]["decision_code"] == "VERIFIED_SUCCESS"

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.arbitrator import DisputeArbitratorAgent
from student_agent.contracts import Contracts
from student_agent.llm_client import SmallLLMClient
from student_agent.trace import TraceWriter
from student_agent.verifier import VerifierAgent


class MockLLMClient(SmallLLMClient):
    """Mock LLM client returning controlled JSON responses for testing."""

    def __init__(self, response_data: dict[str, Any] | None) -> None:
        super().__init__()
        self._mock_response = response_data

    def is_available(self) -> bool:
        return True

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        return self._mock_response


def test_llm_arbitrator_fallback_when_unavailable() -> None:
    async def run_test() -> None:
        client = SmallLLMClient()
        arbitrator = DisputeArbitratorAgent(client)

        case = {
            "case_id": "L3B_TEST_001",
            "customer_request": {
                "claims": [{"claim_id": "c1", "topic": "unsupported_claim"}],
                "message": "test claim",
            },
        }
        entity_res = {
            "resolved_order_ids": ["ord_1"],
            "rejected_candidates": [],
            "status": "resolved",
        }
        order_res = {
            "order": {"order_status": "delivered"},
            "seller_ids": ["s1"],
            "item_ids": ["i1"],
        }
        ship_res = {"verdict": "on_time", "late_seller_ids": [], "timeline_complete": True}
        pay_res = {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refundable_total_brl": 0.0,
        }
        policy_rules = {
            "unsupported_claim": {
                "case_status": "no_action",
                "recommended_action": "document_no_action",
                "refund_brl": 0.0,
                "responsible_parties": [{"party_type": "customer", "party_id": None}],
            }
        }

        result = await arbitrator.arbitrate(
            case=case,
            entity_res=entity_res,
            order_res=order_res,
            ship_res=ship_res,
            pay_res=pay_res,
            policy_rules=policy_rules,
            evidence_refs=["ev_123456789012345678901234"],
        )

        assert result["assessment"]["primary_issue"] == "unsupported_claim"
        assert result["assessment"]["case_status"] == "no_action"
        assert result["financial_resolution"]["recommended_refund_brl"] == 0.0

    asyncio.run(run_test())


def test_llm_arbitrator_with_mock_llm_decision(tmp_path: Path) -> None:
    async def run_test() -> None:
        mock_data = {
            "primary_issue": "late_delivery_logistics",
            "case_status": "action_required",
            "confidence": 0.98,
            "data_conflicts": [
                {
                    "field": "delivery_date",
                    "sources": ["customer_claim", "shipment_timeline"],
                    "selected_source": "shipment_timeline",
                    "resolution_code": "CARRIER_SLA_ENFORCED",
                }
            ],
        }
        mock_llm = MockLLMClient(mock_data)
        arbitrator = DisputeArbitratorAgent(mock_llm)

        case = {
            "case_id": "L3B_TEST_002",
            "customer_request": {
                "claims": [{"claim_id": "c1", "topic": "late_delivery_seller"}],
                "message": "Arrived very late, carrier delayed it.",
            },
        }
        entity_res = {
            "resolved_order_ids": ["ord_2"],
            "rejected_candidates": [],
            "status": "resolved",
        }
        order_res = {
            "order": {"order_status": "delivered"},
            "seller_ids": ["s1"],
            "item_ids": ["i1"],
        }
        ship_res = {"verdict": "logistics_delay", "late_seller_ids": [], "timeline_complete": True}
        pay_res = {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refundable_total_brl": 0.0,
        }
        policy_rules = {
            "late_delivery_seller": {
                "case_status": "action_required",
                "recommended_action": "penalize_seller",
                "refund_brl": 25.0,
                "responsible_parties": [{"party_type": "seller", "party_id": "s1"}],
            },
            "late_delivery_logistics": {
                "case_status": "action_required",
                "recommended_action": "refund_freight",
                "refund_brl": 15.0,
                "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
            },
        }

        result = await arbitrator.arbitrate(
            case=case,
            entity_res=entity_res,
            order_res=order_res,
            ship_res=ship_res,
            pay_res=pay_res,
            policy_rules=policy_rules,
            evidence_refs=["ev_123456789012345678901234"],
        )

        # LLM correctly overrode customer's claim topic to logistics delay
        assert result["assessment"]["primary_issue"] == "late_delivery_logistics"
        assert result["assessment"]["confidence"] == 0.98
        assert result["financial_resolution"]["recommended_refund_brl"] == 15.0
        assert len(result["data_conflicts"]) == 1
        assert result["data_conflicts"][0]["resolution_code"] == "CARRIER_SLA_ENFORCED"

        # Verify that Verifier passes this output
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        verifier = VerifierAgent(contracts, trace)
        verified = verifier.verify_and_calibrate(result, case)
        assert verified["assessment"]["primary_issue"] == "late_delivery_logistics"

    asyncio.run(run_test())

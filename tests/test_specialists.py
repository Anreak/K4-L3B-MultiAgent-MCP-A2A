from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from student_agent.contracts import Contracts
from student_agent.specialists import (
    CaseGateway,
    EntityAgent,
)
from student_agent.trace import TraceWriter


def test_case_gateway_caching_and_evidence(tmp_path: Path) -> None:
    async def run_test() -> None:
        mock_gateway = AsyncMock()
        mock_gateway.call.return_value = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_test_1234567890123456789012",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"order_id": "ord_1"},
        }
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

        cgw = CaseGateway(mock_gateway, "L3B_CASE_TEST", trace)

        # First call
        ev1 = await cgw.call("get_order", actor="order-agent", order_id="ord_1")
        assert ev1["evidence_ref"] == "ev_test_1234567890123456789012"
        assert mock_gateway.call.call_count == 1

        # Second identical call should hit in-memory cache
        ev2 = await cgw.call("get_order", actor="order-agent", order_id="ord_1")
        assert ev2["evidence_ref"] == "ev_test_1234567890123456789012"
        assert mock_gateway.call.call_count == 1  # Not incremented due to cache

        assert cgw.evidence_refs == ["ev_test_1234567890123456789012"]

    asyncio.run(run_test())


def test_entity_agent_resolution(tmp_path: Path) -> None:
    async def run_test() -> None:
        mock_gateway = AsyncMock()
        mock_gateway.call.return_value = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_customer_hist_1234567890123",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "customer",
            "data": {
                "customer_unique_id": "cust_1",
                "orders": [{"order_id": "real_ord_1"}],
            },
        }
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

        cgw = CaseGateway(mock_gateway, "L3B_CASE_TEST", trace)
        agent = EntityAgent(cgw)

        res = await agent.resolve(
            candidate_order_ids=["real_ord_1", "candidate-001"],
            customer_unique_id_hint="cust_1",
            claimed_order_id="real_ord_1",
        )

        assert res["status"] == "resolved"
        assert res["resolved_order_ids"] == ["real_ord_1"]
        assert res["rejected_candidates"] == ["candidate-001"]
        assert res["confidence"] == 0.95

    asyncio.run(run_test())

from __future__ import annotations

from typing import Any

from .contracts import Contracts
from .trace import TraceWriter


class VerifierAgent:
    """Specialist responsible for cross-field consistency verification."""

    def __init__(self, contracts: Contracts, trace: TraceWriter) -> None:
        self.contracts = contracts
        self.trace = trace

    def verify_and_calibrate(self, output: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]

        # 1. Schema check
        self.contracts.validate_output(output, f"verifier for {case_id}")

        # 2. Case ID invariant
        if output.get("case_id") != case_id:
            raise ValueError(f"Case ID mismatch: expected {case_id}, got {output.get('case_id')}")

        # 3. Entity resolution disjointness
        resolved = set(output["entity_resolution"]["resolved_order_ids"])
        rejected = set(output["entity_resolution"]["rejected_candidates"])
        if resolved & rejected:
            raise ValueError(
                f"Entity resolution overlap: resolved {resolved} intersects rejected {rejected}"
            )

        # 4. Shipment seller delay consistency
        shipment_verdict = output["shipment_analysis"]["verdict"]
        late_sellers = output["shipment_analysis"]["late_seller_ids"]
        if shipment_verdict == "seller_delay" and not late_sellers:
            raise ValueError(
                "Shipment analysis error: verdict is seller_delay but late_seller_ids is empty"
            )
        if shipment_verdict in ("on_time", "logistics_delay") and late_sellers:
            raise ValueError(
                f"Shipment analysis error: verdict is {shipment_verdict} but has late_sellers"
            )

        # 5. Financial resolution balance
        fin = output["financial_resolution"]
        rec_refund = fin["recommended_refund_brl"]
        lines_sum = round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2)
        if round(rec_refund, 2) != lines_sum:
            raise ValueError(
                f"Financial mismatch: recommended {rec_refund} != lines sum {lines_sum}"
            )

        case_status = output["assessment"]["case_status"]
        if case_status == "no_action" and (rec_refund != 0.0 or fin["refund_lines"]):
            raise ValueError(
                "Financial resolution inconsistency: case_status is no_action but refund is > 0"
            )

        # 6. Confidence Calibration check
        conf = output["assessment"]["confidence"]
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"Invalid confidence score: {conf}")

        if output.get("data_conflicts") or case_status == "needs_investigation":
            # Cap confidence if conflict exists
            output["assessment"]["confidence"] = min(conf, 0.70)
        else:
            output["assessment"]["confidence"] = max(conf, 0.90)

        # 7. Evidence refs non-empty
        if not output.get("evidence_refs"):
            raise ValueError("Evidence refs cannot be empty")

        # Emit verification_completed trace event
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="VERIFIED_SUCCESS",
            attributes={
                "primary_issue": output["assessment"]["primary_issue"],
                "case_status": output["assessment"]["case_status"],
                "confidence": output["assessment"]["confidence"],
            },
        )

        return output

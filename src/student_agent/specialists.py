from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class CaseGateway:
    """Scoped gateway wrapper ensuring case_id integrity, in-memory caching, and trace auditing.
    
    Principles strictly enforced:
    1. case_id is immutable per case instance.
    2. evidence_refs are preserved exactly as returned by MCP.
    3. Caches calls within the case scope to optimize efficiency budget.
    4. Automatically emits or registers tool_result_consumed events.
    """

    def __init__(self, gateway: EvidenceGateway, case_id: str, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.case_id = case_id
        self.trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
        self.evidence_refs: list[str] = []

    async def call(
        self,
        tool_name: str,
        *,
        actor: str,
        emit_trace: bool = True,
        max_retries: int = 2,
        **arguments: Any,
    ) -> dict[str, Any]:
        """Call an MCP tool with scoped case_id, caching, and retry mechanism."""
        # Normalize arguments to create a deterministic cache key
        norm_args = tuple(sorted((k, str(v)) for k, v in arguments.items()))
        cache_key = (tool_name, norm_args)

        if cache_key in self._cache:
            evidence = self._cache[cache_key]
            ref = evidence.get("evidence_ref")
            if ref and ref not in self.evidence_refs:
                self.evidence_refs.append(ref)
            if emit_trace and ref:
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[ref],
                )
            return evidence

        # Retry logic for transient MCP network issues
        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                evidence = await self.gateway.call(
                    tool_name,
                    case_id=self.case_id,
                    **{k: str(v) for k, v in arguments.items()},
                )
                self._cache[cache_key] = evidence
                ref = evidence.get("evidence_ref")
                if ref and ref not in self.evidence_refs:
                    self.evidence_refs.append(ref)
                if emit_trace and ref:
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor=actor,
                        tool_name=tool_name,
                        evidence_refs=[ref],
                    )
                return evidence
            except Exception as exc:
                last_exception = exc
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                else:
                    break

        raise last_exception or RuntimeError(f"Tool {tool_name} failed unexpectedly")


class EntityAgent:
    """Specialist responsible for resolving candidate orders and customer context."""

    def __init__(self, case_gateway: CaseGateway) -> None:
        self.gateway = case_gateway

    async def resolve(
        self,
        candidate_order_ids: list[str],
        customer_unique_id_hint: str | None,
        claimed_order_id: str | None,
    ) -> dict[str, Any]:
        customer_unique_id = customer_unique_id_hint
        resolved_order_ids: list[str] = []
        rejected_candidates: list[str] = []
        customer_history_data: dict[str, Any] = {}

        if customer_unique_id:
            try:
                hist_res = await self.gateway.call(
                    "get_customer_history",
                    actor="entity-agent",
                    customer_unique_id=customer_unique_id,
                )
                customer_history_data = hist_res.get("data", {})
            except Exception:
                customer_history_data = {}

        known_orders = {
            order["order_id"]
            for order in customer_history_data.get("orders", [])
            if "order_id" in order
        }

        for cand in candidate_order_ids:
            if cand in known_orders or (claimed_order_id and cand == claimed_order_id and not cand.startswith("candidate-")):
                if cand not in resolved_order_ids:
                    resolved_order_ids.append(cand)
            else:
                if cand not in rejected_candidates:
                    rejected_candidates.append(cand)

        status = "resolved" if resolved_order_ids else ("ambiguous" if candidate_order_ids else "not_found")
        confidence = 0.95 if status == "resolved" else 0.50

        return {
            "status": status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": confidence,
            "customer_unique_id": customer_unique_id,
            "related_order_ids": resolved_order_ids if resolved_order_ids else [],
            "customer_history": customer_history_data,
        }


class OrderAgent:
    """Specialist responsible for order details, items, sellers, and product context."""

    def __init__(self, case_gateway: CaseGateway) -> None:
        self.gateway = case_gateway

    async def investigate(
        self,
        order_id: str,
        include_sellers: bool = True,
        include_product_context: bool = True,
    ) -> dict[str, Any]:
        order_res = await self.gateway.call("get_order", actor="order-agent", order_id=order_id)
        items_res = await self.gateway.call("get_order_items", actor="order-agent", order_id=order_id)

        sellers_res = {}
        if include_sellers:
            try:
                sellers_res = await self.gateway.call("get_sellers", actor="order-agent", order_id=order_id)
            except Exception:
                sellers_res = {}

        products_res = {}
        if include_product_context:
            try:
                products_res = await self.gateway.call("get_product_context", actor="order-agent", order_id=order_id)
            except Exception:
                products_res = {}

        order_data = order_res.get("data", {})
        items_data = items_res.get("data", [])
        sellers_data = sellers_res.get("data", []) if include_sellers else []
        products_data = products_res.get("data", []) if include_product_context else []

        item_ids = [item["order_item_id"] for item in items_data if "order_item_id" in item]
        seller_ids = [s["seller_id"] for s in sellers_data if "seller_id" in s]
        if not seller_ids:
            seller_ids = [item["seller_id"] for item in items_data if "seller_id" in item]

        return {
            "order": order_data,
            "items": items_data,
            "sellers": sellers_data,
            "products": products_data,
            "item_ids": sorted(list(set(item_ids))),
            "seller_ids": sorted(list(set(seller_ids))),
        }


class ShipmentAgent:
    """Specialist responsible for logistics, shipping milestones, and delivery delays."""

    def __init__(self, case_gateway: CaseGateway) -> None:
        self.gateway = case_gateway

    async def investigate(self, order_id: str) -> dict[str, Any]:
        shipment_res = await self.gateway.call(
            "get_shipment_summary",
            actor="shipment-agent",
            order_id=order_id,
        )
        shipment_data = shipment_res.get("data", {})

        order_status = shipment_data.get("order_status")
        delivered_cust = shipment_data.get("delivered_customer_at")
        estimated_delivery = shipment_data.get("estimated_delivery_at")
        events = shipment_data.get("events", [])
        shipping_limits = shipment_data.get("shipping_limits", [])

        timeline_complete = bool(delivered_cust and estimated_delivery)

        # Detect late sellers from events and shipping limits
        late_sellers: set[str] = set()
        has_seller_delay_event = False
        has_logistics_delay_event = False

        for ev in events:
            if ev.get("event_type") == "delivered_late":
                if ev.get("actor") == "seller":
                    has_seller_delay_event = True
                elif ev.get("actor") == "logistics_provider":
                    has_logistics_delay_event = True

        if has_seller_delay_event:
            for limit in shipping_limits:
                if limit.get("seller_id"):
                    late_sellers.add(limit["seller_id"])

        if has_seller_delay_event:
            verdict = "seller_delay"
        elif has_logistics_delay_event:
            verdict = "logistics_delay"
        elif delivered_cust and estimated_delivery:
            if delivered_cust > estimated_delivery:
                verdict = "logistics_delay"
            else:
                verdict = "on_time"
        elif order_status in ("canceled", "unavailable"):
            verdict = "on_time"
        else:
            verdict = "on_time"

        return {
            "verdict": verdict,
            "late_seller_ids": sorted(list(late_sellers)),
            "timeline_complete": timeline_complete,
            "shipment_data": shipment_data,
        }


class PaymentAgent:
    """Specialist responsible for payments, installment breakdowns, and refunds."""

    def __init__(self, case_gateway: CaseGateway) -> None:
        self.gateway = case_gateway

    async def investigate(
        self,
        order_id: str,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        claim_topics = {c.get("topic") for c in claims}
        has_refund_claim = bool(claim_topics & {"refund_pending", "refund_failed"})

        timeline_res = await self.gateway.call("get_payment_timeline", actor="payment-agent", order_id=order_id)
        ref_res = None
        if has_refund_claim:
            try:
                ref_res = await self.gateway.call("get_refund_timeline", actor="payment-agent", order_id=order_id)
            except Exception:
                ref_res = None

        payment_timeline_data = timeline_res.get("data", {})
        payments_data = payment_timeline_data.get("payments", [])
        refund_data = ref_res.get("data", {}) if ref_res else None

        captured_total = 0.0
        events = payment_timeline_data.get("events", [])
        for ev in events:
            if ev.get("event_type") == "captured":
                try:
                    captured_total += float(ev.get("amount_brl", 0.0))
                except (ValueError, TypeError):
                    pass

        if captured_total == 0.0 and payments_data:
            for p in payments_data:
                try:
                    captured_total += float(p.get("payment_value", 0.0))
                except (ValueError, TypeError):
                    pass

        refunded_total = 0.0
        has_pending_refund = False
        has_failed_refund = False

        if refund_data:
            for rev in refund_data.get("events", []):
                st = rev.get("status")
                amt = float(rev.get("amount_brl", 0.0))
                if st == "confirmed":
                    refunded_total += amt
                elif st == "pending":
                    has_pending_refund = True
                elif st == "failed":
                    has_failed_refund = True

        refundable_total = max(0.0, round(captured_total - refunded_total, 2))

        # Check for duplicate captures
        is_duplicate = False
        if len(payments_data) >= 4:
            # Check sequential duplicates
            seqs = [p.get("payment_sequential") for p in payments_data]
            if len(seqs) != len(set(seqs)) and len(set(seqs)) <= len(seqs) // 2:
                is_duplicate = True

        if has_failed_refund:
            verdict = "refund_failed"
        elif has_pending_refund:
            verdict = "refund_pending"
        elif is_duplicate:
            verdict = "duplicate_capture"
        elif "payment_mismatch" in claim_topics:
            verdict = "capture_mismatch"
        else:
            verdict = "reconciled"

        payment_references = list(
            dict.fromkeys(
                f"pay_{p.get('payment_sequential', i+1)}_{i+1}_{p.get('payment_type', 'unknown')}"
                for i, p in enumerate(payments_data)
            )
        )

        return {
            "verdict": verdict,
            "captured_total_brl": round(captured_total, 2),
            "refunded_total_brl": round(refunded_total, 2),
            "refundable_total_brl": refundable_total,
            "payment_references": payment_references,
            "payments_data": payments_data,
            "refund_data": refund_data,
        }


class PolicyAgent:
    """Specialist responsible for fetching platform policies and determining SLA rules."""

    def __init__(self, case_gateway: CaseGateway) -> None:
        self.gateway = case_gateway

    async def get_policy_rules(self, policy_version: str) -> dict[str, Any]:
        policy_res = await self.gateway.call(
            "get_policy",
            actor="policy-agent",
            policy_version=policy_version,
        )
        return policy_res.get("data", {}).get("rules", {})

# L3B Architecture Record

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (K4 L3B Multi-Agent MCP + A2A). Mọi quyết định thiết kế đều hướng đến việc tối ưu hoá độ chính xác nghiệp vụ (semantic), kiểm soát bằng chứng minh bạch (evidence provenance), tuân thủ nghiêm ngặt schema và tối ưu hiệu suất gọi tool qua MCP Gateway.

---

## 1. System Overview

Hệ thống được thiết kế theo mô hình **Hierarchical Multi-Agent Architecture with Specialist Agents and Guardrail Verifier**:

```text
               ┌────────────────────────────────────────────────────────┐
               │                     Input Case JSON                    │
               └───────────────────────────┬────────────────────────────┘
                                           │ (case_received)
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │                   Coordinator Agent                    │
               └───────────┬────────────────────────────────┬───────────┘
                           │ (task_assigned)                │
                           ▼                                │
               ┌───────────────────────┐                    │
               │ Entity Resolver Agent │                    │
               └───────────┬───────────┘                    │
                           │ (handoff: resolved entity)     │
                           ▼                                │
               ┌─────────────────────────────────────────┐  │
               │   Specialist Delegation & Investigation │◄─┘
               └──────┬──────────────────┬──────────────┬┘
                      │                  │              │
                      ▼                  ▼              ▼
         ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
         │  Shipment Agent  │  │ Payment / Refund │  │  Order / Product │
         │   (Logistics)    │  │     (Finance)    │  │    (Catalog)     │
         └────────────┬─────┘  └────────┬─────────┘  └──────────┬───────┘
                      │                 │                       │
                      └─────────────────┼───────────────────────┘
                                        │
                                        ▼
                           ┌─────────────────────────┐
                           │   Policy Agent / A2A    │
                           │    (policy_decided)     │
                           └────────────┬────────────┘
                                        │
                                        ▼
                           ┌─────────────────────────┐
                           │ Conflict Resolver Agent │
                           └────────────┬────────────┘
                                        │ (reconciled context)
                                        ▼
                           ┌─────────────────────────┐
                           │     Verifier Agent      │
                           │ (verification_completed)│
                           └────────────┬────────────┘
                                        │
                                        ▼
               ┌────────────────────────────────────────────────────────┐
               │              Coordinator: Final Output                 │
               │                    (case_finalized)                    │
               └────────────────────────────────────────────────────────┘

 [Audit & Observability Layer]
   ├── MCP Gateway: In-memory Case Cache + Evidence Integrity
   └── TraceWriter: Append-only JSONL event stream (trace-event-v1)
```

### Luồng xử lý chi tiết:
1. **Tiếp nhận (`coordinator`)**: Ghi nhận sự kiện `case_received`, trích xuất thông tin khiếu nại, danh sách ứng viên đơn hàng (`candidates`) và ngữ cảnh khách hàng.
2. **Khử mơ hồ thực thể (`entity_resolver`)**: Truy vấn MCP để đối soát lịch sử khách hàng và chi tiết đơn hàng, xếp hạng candidate, tách bạch `resolved_order_ids` và `rejected_candidates`. Handoff dữ liệu đã giải quyết về cho `coordinator`.
3. **Điều tra chuyên biệt (Specialists)**:
   - `order_product_agent`: Xác thực đơn hàng, mặt hàng (`items`), nhà bán hàng (`sellers`).
   - `shipment_agent`: Phân tích tiến độ vận chuyển, deadline giao hàng, đối soát chậm trễ do seller hay logistics.
   - `payment_refund_agent`: Đối soát dòng tiền (`captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`), phát hiện duplicate capture hoặc lỗi refund.
4. **Đối chiếu chính sách (`policy_agent`)**: Tra cứu chính sách nền tảng tương ứng với mã khiếu nại, phát sinh sự kiện `policy_decided`.
5. **Hòa giải xung đột (`conflict_resolver`)**: Đối chiếu chéo dữ liệu giữa các bên (người mua - người bán - đơn vị vận chuyển - cổng thanh toán). Nếu có sai lệch, áp dụng quy tắc phân cấp ưu tiên nguồn để ghi nhận `data_conflicts`.
6. **Kiểm tra bất biến (`verifier`)**: Kiểm tra toàn diện schema, tính nhất quán tài chính, tính hợp lệ của bằng chứng, và độ tin cậy (`confidence`). Phát sinh sự kiện `verification_completed`.
7. **Đóng gói kết quả (`coordinator`)**: Ghi file `outputs/<case_id>.json` và emit sự kiện `case_finalized`.

---

## 2. Agent Ownership

Hệ thống áp dụng nguyên tắc **Đặc quyền tối thiểu (Least Privilege)**: Từng Agent chỉ được phép gọi các công cụ MCP phù hợp với miền nghiệp vụ của mình và chỉ được ghi nhận các sự kiện trace tương ứng.

| Actor | Input | Trách nhiệm | Tool permission | Output / Handoff |
| :--- | :--- | :--- | :--- | :--- |
| **`coordinator`** | Case input thô | Khởi tạo vòng đời case, phân rã bài toán, ủy quyền nhiệm vụ theo DAG, tổng hợp kết quả cuối cùng. | *None* (Không gọi trực tiếp MCP, chỉ điều phối qua A2A). | Handoff task tới Entity Resolver & Specialists; xuất output file và emit `case_finalized`. |
| **`entity_resolver`** | Customer info, candidate order IDs, claim text | Khử mơ hồ đơn hàng/khách hàng, phân loại resolved vs rejected candidates, xác định `customer_unique_id`. | `get_customer_history`, `get_order` | Handoff `entity_context` (resolved orders, rejected orders, confidence) về `coordinator`. |
| **`order_product_agent`** | Resolved order IDs, item references | Truy xuất chi tiết sản phẩm, trạng thái hủy/hết hàng (`canceled_order_paid`, `unavailable_order_paid`), thông tin người bán. | `get_order`, `get_order_items`, `get_product_context`, `get_sellers` | Trả về thông tin chi tiết mặt hàng, danh sách seller IDs, trạng thái đơn hàng. |
| **`shipment_agent`** | Resolved order IDs, estimated delivery dates | Phân tích toàn trình vận chuyển, kiểm tra mốc thời gian giao hàng, xác định lỗi trễ do seller (`late_seller_ids`) hay do đối tác vận chuyển. | `get_shipment_summary` | Trả về `shipment_analysis` (`verdict`, `late_seller_ids`, `timeline_complete`). |
| **`payment_refund_agent`** | Resolved order IDs, payment references | Phân tích thanh toán, kiểm tra giao dịch trùng, sai lệch giá trị, trạng thái hoàn tiền, tính toán hạn mức có thể hoàn lại. | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Trả về `payment_analysis` (`captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`, `verdict`). |
| **`policy_agent`** | Issue code, order context, delivery timestamps | Đối chiếu điều khoản bảo hành, đổi trả, bồi thường của sàn thương mại điện tử. | `get_policy` | Phát sinh sự kiện `policy_decided`, trả về điều khoản áp dụng và hướng xử lý tài chính hợp lệ. |
| **`conflict_resolver`** | Dữ liệu tổng hợp từ các specialist agents | So khớp dữ liệu chéo nguồn, phát hiện bất đồng thông tin giữa tracking vs payment vs order status. | *None* (Pure algorithmic reconciliation). | Trả về mảng `data_conflicts` chuẩn hóa và context đã hòa giải. |
| **`verifier`** | Bản thảo output JSON, trace events, evidence refs | Kiểm chứng toàn bộ Invariants, Schema V2, tính toàn vẹn tài chính và bằng chứng trước khi xuất bản. | *None* (Pure validation & guardrails). | Phát sinh sự kiện `verification_completed` kèm quyết định PASS / REVISE. |

---

## 3. Entity Resolution và A2A Protocol

### 3.1. Thuật toán Candidate Ranking & Rejection
Khi một case không cung cấp chính xác `order_id`:
1. **Truy vấn lịch sử**: Sử dụng `customer_unique_id` hoặc mã khách hàng gián tiếp để gọi `get_customer_history`.
2. **So khớp đa tiêu chí (Multi-factor Matching)**:
   - **Khoảng thời gian (Temporal proximity)**: So sánh thời điểm phát sinh khiếu nại với `order_purchase_timestamp`.
   - **Giá trị đơn hàng (Monetary match)**: So sánh số tiền được khiếu nại với tổng tiền đơn hàng.
   - **Danh mục / Sản phẩm (Item overlap)**: Đối chiếu tên/mã sản phẩm được nhắc trong khiếu nại.
3. **Phân loại ngưỡng tự tin (Confidence Thresholding)**:
   - Nếu $Score \ge 0.85$: Đưa vào `resolved_order_ids`.
   - Nếu $0.40 \le Score < 0.85$: Đánh dấu mơ hồ, đưa `status` = `ambiguous`.
   - Nếu $Score < 0.40$: Đưa vào `rejected_candidates`.
   - Trường hợp không có đơn nào phù hợp: `status` = `not_found`, `resolved_order_ids` = `[]`.

### 3.2. Cấu trúc Message Envelope & Correlation
Mọi giao tiếp nội bộ giữa các agent tuân thủ cấu trúc envelope bất biến:
```json
{
  "envelope_version": "1.0",
  "case_id": "L3B_CASE_001",
  "correlation_id": "corr_c8f12a9b-34ef",
  "sender": "coordinator",
  "receiver": "shipment_agent",
  "action": "ANALYZE_SHIPMENT",
  "payload": {
    "order_id": "e481f51cbdc54678b7cc49136f2d6af7"
  },
  "created_at": "2026-09-25T07:30:00Z"
}
```
- Mọi message đều gắn chặt với `case_id`. Nghiêm cấm nhận hoặc xử lý envelope có `case_id` không khớp với context hiện hành.

### 3.3. Điều kiện Handoff & Chống lặp (Loop Avoidance)
- Quy trình ủy quyền tuân thủ đồ thị có hướng không chu trình (**DAG - Directed Acyclic Graph**):
  `Coordinator` $\rightarrow$ `EntityResolver` $\rightarrow$ `Coordinator` $\rightarrow$ `Specialists (Parallel)` $\rightarrow$ `Policy` $\rightarrow$ `ConflictResolver` $\rightarrow$ `Verifier` $\rightarrow$ `Coordinator`.
- Mỗi specialist chỉ được thực thi tối đa **1 lần** chính và **1 lần chỉnh sửa** nếu Verifier từ chối.
- Giới hạn execution timeout cho mỗi sub-task là **15 giây** để ngăn chặn hiện tượng treo tiến trình hoặc deadlock.
- Trace log chỉ ghi nhận các sự kiện quan sát được (`task_assigned`, `handoff`), tuyệt đối không ghi nhận chain-of-thought hay prompt nội bộ.

---

## 4. Evidence và Conflict Lifecycle

### 4.1. Thu thập & Xác thực MCP Evidence
1. Mọi lệnh gọi MCP Gateway đều trả về cấu trúc tuân thủ [`mcp-evidence-response-v1.schema.json`](file:///d:/VInCode/K4-L3B-MultiAgent-MCP-A2A/contracts/schemas/mcp-evidence-response-v1.schema.json).
2. Gateway tự động kiểm tra `result_hash` (SHA-256) và `evidence_ref` (định dạng `ev_[A-Za-z0-9_-]{20,96}`).
3. Mọi `evidence_ref` hợp lệ được lưu trong `CaseEvidenceLedger` riêng biệt của từng case.
4. **Phòng chống nhiễm chéo (Cross-scope Isolation)**: `CaseEvidenceLedger` được dọn sạch giữa các case. Tuyệt đối không tái sử dụng `evidence_ref` của case khác (tránh Hard Gate vi phạm 0 điểm).

### 4.2. Thứ tự ưu tiên nguồn tin (Source Precedence Policy)
Khi phát hiện mâu thuẫn dữ liệu giữa các nguồn, `conflict_resolver` áp dụng thứ tự ưu tiên pháp lý/kỹ thuật như sau:
1. **Level 1 (Highest) - Immutable Audit Logs**: Nhật ký thanh toán ngân hàng (`payment_timeline`) và mốc thời gian bưu cục xác nhận (`shipment_summary` carrier timestamps).
2. **Level 2 - Platform System State**: Trạng thái đơn hàng trên hệ thống sàn thương mại điện tử (`get_order`, `get_order_payments`).
3. **Level 3 - Merchant / Seller Statements**: Dữ liệu do nhà bán hàng tự khai báo (`get_sellers`).
4. **Level 4 (Lowest) - Customer Self-reported Claim**: Lời khai của khách hàng trong nội dung khiếu nại (cần có bằng chứng Level 1-3 hỗ trợ mới được công nhận).

### 4.3. Biểu diễn mâu thuẫn dữ liệu (`data_conflicts`)
- Nếu sai lệch giữa Level cao hơn và Level thấp hơn: Chọn giá trị của Level cao hơn, ghi nhận `data_conflict` với `selected_source` là nguồn ưu tiên và `resolution_code` = `"PRECEDENCE_RULE_APPLIED"`.
- Nếu sai lệch giữa hai nguồn cùng Level mà không thể hòa giải: Đặt `selected_source` = `null`, `resolution_code` = `"UNRESOLVABLE_DISCREPANCY"`, đồng thời hạ `assessment.confidence` và chuyển `case_status` = `"needs_investigation"`.

### 4.4. Ánh xạ Evidence vào Trace (`tool_result_consumed`)
- Khi một agent trích xuất thuộc tính từ một `evidence_ref` để đưa ra kết luận (hoặc quyết định hành động), agent phải emit sự kiện `tool_result_consumed` với mảng `evidence_refs` chứa đúng mã bằng chứng đó.
- Danh sách `evidence_refs` ở cấp cao nhất của output JSON phải là tập hợp hợp lệ của tất cả các `evidence_refs` đã được tiêu thụ trong trace.

---

## 5. Failure and Efficiency Policy

### 5.1. Bảng chính sách xử lý sự cố (Failure Handling Matrix)

| Loại sự cố (Failure Mode) | Ngân sách Retry | Chiến lược Fallback | Trace Event / Decision Code |
| :--- | :---: | :--- | :--- |
| **MCP Timeout / 5xx Error** | 2 lần (Exponential backoff: 1s, 2s) | Cô lập tool bị lỗi, đánh dấu phân hệ là `insufficient_evidence`, tiếp tục các phân hệ khác. | `tool_result_consumed` với attribute `error: "mcp_timeout"` |
| **Entity Not Found / Ambiguous** | 1 lần (Thử query bằng ID phụ nếu có) | Đặt `entity_resolution.status` = `"ambiguous"` hoặc `"not_found"`, gán `case_status` = `"needs_investigation"`. Không bịa đặt order ID. | `handoff` với `decision_code`: `"ENTITY_RESOLUTION_AMBIGUOUS"` |
| **Source Conflict không thể hòa giải** | 0 lần (Deterministic) | Ghi nhận vào `data_conflicts`, hạ `confidence` $\le 0.65$, yêu cầu can thiệp con người (`needs_investigation`). | `policy_decided` với `decision_code`: `"SOURCE_CONFLICT_DETECTED"` |
| **Verifier Invariant Violation** | 1 lần | Yêu cầu Specialist có trường lỗi tính toán lại; nếu vẫn lỗi, áp dụng fallback an toàn (refund = 0, no_action). | `verification_completed` với `decision_code`: `"INVARIANT_FIX_RETRY"` |

### 5.2. Quản lý ngân sách gọi Tool (Query Budget & Cache Strategy)
- **Per-Case In-Memory Cache**: Mọi cuộc gọi đến `gateway.call(tool_name, **arguments)` được lưu cache theo khóa `(tool_name, frozenset(arguments.items()))`. Nếu cùng một case yêu cầu dữ liệu đã lấy trước đó, trả về từ cache ngay lập tức, không gọi lại MCP Gateway.
- **Per-Case Query Budget**: Mỗi case chỉ được thực hiện tối đa **6-8 MCP tool calls**.
- **Chỉ gọi theo nhu cầu (Demand-driven Investigation)**:
  - Nếu khiếu nại thuần túy về giao hàng chậm: Không gọi `get_refund_timeline` hay `get_product_context`.
  - Nếu khiếu nại về trừ tiền 2 lần: Tập trung vào `get_order_payments` và `get_payment_timeline`, không truy vấn sâu vào thông tin seller.
- Mục tiêu đạt điểm tuyệt đối cho tiêu chí `efficiency` (5% tổng điểm cuộc thi).

---

## 6. Verification Invariants

Trước khi hàm `solve_case` hoàn tất và ghi ra file `outputs/<case_id>.json`, `verifier` phải kiểm tra các bất biến sau. Nếu vi phạm bất kỳ bất biến nào, output sẽ bị chặn lại để sửa chữa:

1. **Schema Compliance**:
   - Output phải thỏa mãn 100% định dạng JSON Schema của `day09-l3b-output-v2`. Không thừa thuộc tính (`additionalProperties: false`), không thiếu trường bắt buộc.
2. **Case ID Uniformity**:
   - Trường `case_id` trong output phải khớp tuyệt đối với `case_id` trong input.
3. **Entity Scope & Candidate Partitioning**:
   - Tất cả các entity IDs (`order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`) phải được tìm thấy trong evidence đã thu thập.
   - `resolved_order_ids` và `rejected_candidates` phải rời nhau hoàn toàn:
     $$\text{resolved\_order\_ids} \cap \text{rejected\_candidates} = \emptyset$$
4. **Evidence Provenance Integrity**:
   - Mọi mã trong mảng `evidence_refs` phải bắt đầu bằng tiền tố `ev_` và phải nằm trong tập hợp các evidence mà MCP Gateway đã thực sự trả về trong quá trình chạy case này.
5. **Shipment Logic Invariant**:
   - Nếu `shipment_analysis.verdict` = `"seller_delay"`, thì `late_seller_ids` bắt buộc phải chứa ít nhất một seller ID hợp lệ.
   - Nếu `shipment_analysis.verdict` thuộc `{"on_time", "logistics_delay"}`, `late_seller_ids` phải là mảng rỗng `[]`.
6. **Financial Balance Invariants**:
   - $\text{captured\_total\_brl} \ge 0$ và $\text{refunded\_total\_brl} \ge 0$.
   - Giới hạn khả năng hoàn tiền:
     $$\text{refundable\_total\_brl} \le \max(0.0, \, \text{captured\_total\_brl} - \text{refunded\_total\_brl})$$
   - Số tiền đề xuất hoàn phải bằng tổng các dòng hoàn tiền chi tiết:
     $$\text{recommended\_refund\_brl} = \sum_{i} \text{refund\_lines}[i].\text{amount\_brl}$$
   - Nếu `case_status` = `"no_action"`, thì `recommended_refund_brl` bắt buộc phải bằng `0.0` và `refund_lines` phải là `[]`.
7. **Trace Lifecycle Completeness**:
   - Phải đảm bảo tối thiểu 5 sự kiện theo đúng thứ tự:
     `case_received` $\rightarrow$ `task_assigned` $\rightarrow$ `handoff` $\rightarrow$ `verification_completed` $\rightarrow$ `case_finalized`.
8. **Confidence Calibration**:
   - Giá trị `confidence` $\in [0.0, 1.0]$.
   - Nếu `entity_resolution.status` $\neq$ `"resolved"` hoặc có `data_conflicts`, `confidence` không được vượt quá `0.70`.

---

## 7. Reproducibility

- **Môi trường & Dependency**:
  - Python: `3.11+` hoặc `3.12+`.
  - Virtual Environment: `.venv` được khởi tạo và cài đặt thông qua `pip install -e ".[dev]"`.
  - Các package cốt lõi: `mcp>=2,<3`, `httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`.
- **Cấu hình thực thi**:
  - Lệnh kiểm tra schema input: `day09 validate-inputs`
  - Lệnh liệt kê MCP tools: `day09 mcp-tools`
  - Lệnh chạy toàn bộ 100 cases: `day09 run`
  - Lệnh xác thực output & trace: `day09 validate`
  - Lệnh đóng gói nộp bài: `day09 package --output dist/submission.zip`
- **Quản lý tài nguyên & Concurrency**:
  - Concurrency: Xử lý tuần tự hoặc tối đa 4 worker luồng đồng thời nhằm tuân thủ rate limit và tránh làm nghẽn kết nối MCP Gateway.
  - Timeout: 300 giây cho kết nối HTTP tới Gateway; 30 giây tối đa cho mỗi case riêng lẻ.
  - Tuyệt đối không ghi thông tin bí mật (API Key, Private Tokens) vào bất kỳ tài liệu hay file mã nguồn nào được commit.

# Review of Commit 970dcff (English)

Repository: `https://github.com/kimjundong7080-crypto/drop-shipping-docs.git`
Branch: `main`
Reviewed commit: `970dcff47ab2baf22601760dd8943d2ca29a0ba1` ("Commit all working-tree changes: P2 doc fixes, webhook worker, cost ledger, dispute service, docs reorg")
Review date: 2026-10-22
Method: full diff against `b0b28d2`, source inspection, and local execution of the affected paths. Live provider APIs were not called (no credentials).

Note: the commit initially reported as `5912d0a` does not exist in this repository. The current `origin/main` is `970dcff`, and this review targets that commit.

---

## 1. Verdict

**Partially fixed — not safe for live orders.**

The commit fixes the audit-chain concurrency defect, the webhook duplicate-detection fail-open, the import-time Shopify client in `listing_service`, webhook port configuration, and CLI initialization order. It also adds a webhook worker, a cost ledger, and a dispute service.

However, several previously reported P0 defects remain, and the commit introduces two new high-severity regressions:

1. **`.gitignore` no longer excludes databases, virtualenvs, and logs**, so `dropship.db` (orders, addresses, emails) is now an untracked candidate for commit.
2. **The HTML order preview is unauthenticated on loopback**, exposing order PII and allowing bypass through a reverse proxy or tunnel that connects from `127.0.0.1`.

In addition, the new cost ledger is never populated because of a payload key mismatch, the margin guard still uses the selling price as cost, and inventory synchronization still pairs variants with the wrong supplier SKU.

---

## 2. Verification matrix

| # | Previous finding | Status in 970dcff | Evidence |
|---|---|---|---|
| P0-1 | Inventory variant/SKU mismatch | **Not fixed** | Re-ran the join test: `V-A -> SKU-A`, `V-B -> SKU-A` |
| P0-2 | Margin guard ineffective / no supplier cost | **Partially fixed, still wrong** | Guard now raises, but a 42.00 free-shipping order is blocked and supplier cost is never used |
| P0-3 | `.99` rounding overprices low-cost items | **Not fixed** | `compute_sell_price(1.00)` returns `1.99` at 35% margin |
| P0-4 | Webhook fail-open with empty secret | **Not fixed** | Forged empty-key CJ HMAC returned HTTP 200 |
| P0-5 | Audit chain forks under concurrency | **Fixed** | 8 threads x 10 appends: 80 rows, `verify_audit_chain() == True` |
| P0-6 | Sandbox/dry-run semantics incorrect | **Not fixed** | `create_order` still ignores `sandbox`; `--sandbox-first` still calls `place_order` |
| P0-7 | Webhook dedup swallows DB errors; no processing | **Fixed / extended** | Only `IntegrityError` is a duplicate; worker consumes `processed=0`; Woo endpoint added |
| P0-8 | `listing_service` import-time Shopify client | **Fixed** | Lazy `_shopify()` accessor added |
| P1-1 | Preview template path is CWD-relative | **Not fixed** | Started from `%TEMP%`: HTTP 500, `TemplateNotFound` |
| P1-2 | `WEBHOOK_BIND_PORT` ignored | **Fixed** | `--port` default is now `None` and falls back to config |
| P1-3 | Supplier input validation / HTML sanitization | **Not fixed** | No changes to `services/normalizer.py` |
| P1-4 | Shopify option values truncated to 3 | **Not fixed** | `clients/shopify_client.py` unchanged in this commit |
| P1-5 | Shopify retry loses `Retry-After` | **Not fixed** | `clients/shopify_client.py` unchanged |
| P1-6 | Inventory stale window unused; delta drift | **Not fixed** | `cutoff` still only logged; Shopify still uses `new_qty - last_pushed_qty` |
| P1-10 | No body-size/rate limits | **Not fixed** | No middleware added |
| P1-12 | Plaintext DB / file permissions | **Not fixed, worsened** | `.gitignore` no longer excludes `*.db` |
| P1-13 | Dependency hashing / audit | **Not fixed** | `requirements.txt` unchanged |

---

## 3. Confirmed fixes

### 3.1 Audit chain concurrency

`utils/audit.py` now opens its own connection and wraps last-hash lookup plus insert in `BEGIN IMMEDIATE` with a 30-second timeout. The prior read/insert race is removed.

Test:

```text
rows: 80
chain valid: True
```

Residual risk: this serializes writers only within SQLite's locking model. Multi-process writers are still serialized by SQLite, but long-running caller transactions can hold the write lock; callers should never call `append_audit` inside an open write transaction.

### 3.2 Webhook duplicate handling and worker

`webhook/receiver.py::_record` now catches only `sqlite3.IntegrityError`; other database errors propagate as HTTP 500, which is the correct fail-closed behavior for provider retries. Raw bodies are stored in `webhook_events.raw_json`, and `webhook/worker.py` consumes `processed = 0` rows. A `POST /webhooks/woo` endpoint with `X-WC-Webhook-Signature` verification was added.

Test: `python main.py webhook-worker --once` consumed one event and set `processed = 1`.

Residual issues:
- Failed events are marked `processed = -1` and are never retried or surfaced in a dead-letter view.
- The worker must be run as a separate process; nothing starts it automatically.
- `_dispatch` only logs; processing happens entirely in the worker.

### 3.3 Startup and CLI corrections

- `init_db()` moved after argument parsing.
- `listing_service` uses a lazy Shopify client.
- `webhook --port` defaults to `WEBHOOK_BIND_PORT`.
- Preview API routes require the API key; OpenAPI endpoints are disabled.
- `--pages` validates a minimum of 1.

---

## 4. Remaining P0 defects

### 4.1 Inventory still maps the wrong supplier SKU

**Location:** `services/inventory_sync.py:43-50` (join) and `:56-65` (dedup)

The commit added an optional `canonical_sku` filter but did not add the missing variant-level predicate. The join is still:

```sql
JOIN mapping m
  ON m.canonical_sku = cv.canonical_sku
 AND m.supplier = 'cj'
 AND m.verified = 1
```

`channel_variants.channel_sku` is never compared with `mapping.supplier_sku`. The first row per `(channel, channel_variant_id)` still wins arbitrarily.

**Reproduction result:**

```text
('shopify', 'V-A') channel_sku=SKU-A -> supplier_sku=SKU-A
('shopify', 'V-B') channel_sku=SKU-B -> supplier_sku=SKU-A
```

**Required fix:** `AND cv.channel_sku = m.supplier_sku`, plus an invariant test with at least two variants.

### 4.2 Margin guard uses the selling price as cost, and the ledger is never written

**Location:** `services/order_service.py:331-345` (guard), `:455-467` (ledger), `:186-240` (payload)

Two separate defects:

1. `cost_total` still sums `order_items.unit_price`, which stores the channel selling price. A free-shipping order whose total equals its subtotal is therefore blocked even when the real margin is healthy. Conversely, shipping or tax can lift `orders.total` above the sales subtotal and let a genuine negative-margin order through.
2. The ledger loop iterates `payload.get("items", [])`, but `_build_cj_payload` returns a `products` list whose entries have only `vid`, `quantity`, and `shippingName`. There is no `items`, `declaredValue`, `cost`, or `sku`, and no `shippingFee`. The loop therefore records nothing.

**Observed:**

```text
42.00 -> BLOCKED (free-shipping order, total == subtotal)
1.00  -> BLOCKED
50.00 -> allowed (actual supplier cost unknown)
cost ledger rows: 0
```

`profit-report` then reports fabricated profitability:

```csv
order_id,revenue,costs,credits,profit,margin,currency
1,50.00,0,0,50.00,1,USD
```

`services/cost_ledger.py::record_from_cj_order` exists but is never called, so CJ's `originalAmount`/`freightAmount` are never persisted.

**Required fix:**
1. Persist supplier cost per SKU (or call `record_from_cj_order` with the `getOrderDetail` response after creation) before using the ledger.
2. Compute the guard from the mapped supplier cost, not from `unit_price`.
3. Iterate the actual payload key (`products`) or, better, the mapped line items with explicit cost fields.
4. Fail the placement when cost data is missing instead of assuming zero cost.
5. Add a ledger test that asserts a non-zero `supplier_cost` after a mocked successful placement.

### 4.3 Webhook signature verification still fails open with an empty secret

**Location:** `utils/security.py:8-22`, `webhook/receiver.py:50-70`

`verify_cj` and `verify_shopify` still build HMACs with `cfg.cj_open_id` / `cfg.shopify_client_secret` without checking that the secret is non-empty, and the webhook app does not validate configuration at startup. The Woo endpoint checks its secret and returns 503, but CJ and Shopify do not.

**Observed:** with no `CJ_OPEN_ID`, `base64(HMAC-SHA256(key=b"", body))` returned HTTP 200.

**Required fix:** reject empty secrets inside each verifier and fail startup when a webhook provider is enabled without its secret.

### 4.4 Pricing rounding unchanged

**Location:** `services/pricing.py:18-24`

`compute_sell_price(Decimal("1.00"))` still returns `1.99` at a 35% margin. The new `pricing_min_margin` lower-bound check in `product_sync` does not address the excessive upper bound.

### 4.5 Sandbox path remains misleading

**Location:** `clients/cj_client.py:156-158`, `services/order_service.py:206-230`, `main.py:68-73`

- `CJClient.create_order(..., sandbox=...)` accepts `sandbox` and ignores it.
- `shippingName` is still set to the supplier SKU.
- `isSandbox` is still an unverified payload field with no separate sandbox base URL.
- `orders place --sandbox-first` still performs a dry run and then calls `place_order`; without `--confirm` it raises `PermissionError`.
- The `cj_orders` PENDING row is still inserted before `preview_order`/payload validation. A non-existent order leaves an orphan PENDING row (confirmed: `local_order_id=999, status=PENDING`).

---

## 5. New regressions introduced by 970dcff

### 5.1 `.gitignore` now exposes the database and runtime artifacts

**Location:** `.gitignore`

The commit removed these entries:

```gitignore
.env
*.db
*.db-wal
*.db-shm
.cj_token.json
logs/
.venv/
venv/
```

and replaced them with only `__pycache__/`, `*.pyc`, `.env`, `.env.local`, plus two deploy-key files.

**Observed:** after the test run, `git status` reports:

```text
?? dropship.db
```

The database contains customer email, shipping address, order amounts, and CJ order records. It must never be committable. `logs/` and `.cj_token.json` are also no longer excluded.

**Required fix:** restore all prior ignore rules immediately and add an explicit test or pre-commit check for tracked/untracked database files.

### 5.2 HTML order preview is unauthenticated on loopback

**Location:** `preview/app.py:20-47`

The previous version applied `Depends(require_api_key)` to the whole app, so `/orders/{id}` required the key. The new `_html_access_ok` returns early without checking any key when `cfg.local_bind_host` is a loopback address.

**Observed:**

```text
GET /orders/1 (no key)      -> 200
GET /api/orders/1 (no key)  -> 401
```

The HTML page contains the customer email, order total, item names, shipping address, and CJ order/tracking data. The code comment argues that a proxy always reports `127.0.0.1`, but that is precisely why the bypass is dangerous: a tunnel or reverse proxy terminating at the loopback address makes remote requests indistinguishable from local ones, and the route then serves PII without a key.

**Required fix:** require the API key (or a signed, short-lived preview token) for HTML routes as well. If browser convenience is required, use a one-time token issued by an authenticated endpoint, not a bind-address check.

### 5.3 Dispute idempotency and refund limit are weak

**Location:** `services/dispute_service.py:66-77`, `:36-42`, `:95-115`

- Idempotency uses substring matching: `if cj_line_id in (r["raw_json"] or "")`. A line ID of `"1"` matches almost any JSON payload, causing legitimate disputes to be skipped.
- `dispute_max_refund_per_order` is applied per dispute, not cumulatively. Multiple disputes under the limit can exceed the intended per-order cap.
- `cj_dispute_id` can be `NULL` when CJ returns an object instead of a scalar; SQLite allows multiple `NULL` values in a `UNIQUE` column, so duplicate rows remain possible.

**Required fix:** store `cj_line_id` in its own indexed column and compare equality; sum existing disputes for the order before applying the guard; require a deterministic dispute key and treat a missing response ID as an uncertain write.

---

## 6. Other observations

- `record_from_cj_order` is dead code. Either call it after a successful CJ order/tracking query or remove it.
- `webhook.worker._handle_cj_logistics` calls `push_tracking` directly. This performs channel fulfillment from an asynchronous webhook; confirm that Shopify/Woo idempotency and approval policy permit it.
- `webhook_events.raw_json` stores raw provider payloads in plaintext with no retention limit, contrary to the supplement's PII/retention requirements.
- `.env.example` still omits `WEBHOOK_BIND_PORT`, `PRICING_DEFAULT_FREIGHT`, `DROPSHIP_DB`, `PRICING_MIN_MARGIN`, and `DISPUTE_MAX_REFUND_PER_ORDER`.
- `tests/` still does not exist; none of the claims in this commit are covered by automated regression tests.
- Korean design documents were moved into `docs/` with no content changes; `docs/MODIFICATION-PLAN.md` and `docs/RUN-REPORT.md` remain valid for the unfixed items.

---

## 7. Recommended next commit

**Blocking:**
1. Restore `.gitignore` rules and remove `dropship.db` from the working tree.
2. Require authentication for HTML preview routes.
3. Fix the inventory join (`cv.channel_sku = m.supplier_sku`).
4. Replace the margin guard's cost source with persisted supplier costs, and fix the ledger payload key.
5. Reject empty webhook secrets at startup and inside each verifier.
6. Implement a real sandbox target or remove the flag, and make `--sandbox-first` a pure dry run.
7. Fix `.99` rounding for low price bands.
8. Resolve the preview template path from `__file__`.

**Then:**
9. Normalizer validation and HTML sanitization.
10. Inventory absolute reconciliation and stale filtering.
11. Dispute idempotency/cumulative limits.
12. Dependency hashing, secret scanning, tests, and CI.

---

## 8. Reproducibility notes

- Environment: Windows, Python 3.15.0 in `.venv`, pinned `requirements.txt` installed.
- Test databases were created under `%TEMP%` and deleted after each check; the project `dropship.db` was removed after the review.
- Servers used for verification were stopped; the working tree is clean apart from this document.
- No live CJ/Shopify/WooCommerce calls were made. Sandbox/dry-run and local HTTP paths were exercised.

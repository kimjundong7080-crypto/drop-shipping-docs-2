# Execution and Revision Report (English)

Project: `drop-shipping-docs` (local drop-shipping automation for CJ Dropshipping, WooCommerce, and Shopify)
Report date: 2026-10-22
Scope: static review of the full source tree plus local execution of every CLI path that does not require live credentials.

Related document: `docs/MODIFICATION-PLAN.md` (required changes, prioritized).

---

## 1. Executive summary

The code is structurally well organized: credentials are read lazily in most modules, SQL is parameterized, webhook HMAC verification uses raw bodies and constant-time comparison, and the preview API is protected by an API key. The pinned dependency set installs and imports successfully.

However, local execution and targeted tests exposed several defects that are not visible from a casual read:

1. **Inventory synchronization can assign the wrong supplier SKU to a channel variant** (confirmed: variant B received variant A's supplier SKU). This can corrupt stock on live stores.
2. **The margin guard cannot work**: it treats the channel selling price as the supplier cost and only logs a warning. A 1.00 sale against 42.00 cost still produced a placement payload.
3. **Webhook verification fails open when a signing secret is unset**: a forged signature computed with an empty key returned HTTP 200.
4. **The audit hash chain forks under concurrent writes** (confirmed: 80 entries, chain invalid).
5. **Pricing rounding applies up to almost 100% markup to low-cost items** (1.00 at 35% margin becomes 1.99).
6. Startup/CLI paths have avoidable failures: `listing` instantiates Shopify at import time, `WEBHOOK_BIND_PORT` is ignored, and the preview template path breaks outside the repository root.

Live integration with CJ, Shopify, and WooCommerce could not be exercised because the environment has no credentials. All findings below are reproducible with local data, mocks, or dry runs.

---

## 2. Test environment

| Item | Value |
|---|---|
| OS | Windows (`win32`), PowerShell 7 |
| Default `python` | 3.15.0 |
| Repository Python range stated in `requirements.txt` | 3.10–3.13 (3.14/3.15 explicitly unverified) |
| Project virtualenv | `.venv` created during this review (gitignored) |
| System site packages | `fastapi 0.145.0`, `starlette 1.8.0`, `uvicorn 0.54.0`, `requests 2.34.2` |
| Pinned install result | `pip install -r requirements.txt` succeeded on Python 3.15.0 |
| Missing from system Python | `tenacity`, `jinja2`, `woocommerce` |
| Credentials | None present (`.env` absent) |

Environment observations:

- The host Python is **outside the documented support range**. The pinned stack did install, but this combination is untested by the project and should either be supported explicitly or blocked with a version guard.
- The system interpreter has newer FastAPI/Starlette than the pins. Commands run with the system interpreter fail on missing packages; results below use `.venv` unless stated otherwise.
- No automated test suite, CI configuration, or linter configuration exists in the repository.

---

## 3. Commands executed

### 3.1 Static and setup

```powershell
python --version
pip --version
python -m pip list --format=freeze
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
python -c "import config, db; db.init_db(); print(db.DB_PATH)"
```

Results: Python 3.15.0; pinned install succeeded; `config`/`db` import and `init_db()` succeeded, creating `dropship.db` under the project root as designed.

### 3.2 CLI smoke tests

| Command | Result | Assessment |
|---|---|---|
| `python main.py --help` | Exit 0, full subcommand list | OK |
| `python main.py audit-verify` | `audit chain valid: True` on an empty DB | OK |
| `python main.py orders preview --order-id 1` | `ValueError: order not found` | Expected with empty DB |
| `python main.py doctor` | `RuntimeError: Missing required env — cj: ...` | Fail-fast works as designed |
| `python main.py listing publish --sku TEST-1` | `RuntimeError` raised during **module import** | Defect (P0-8) |
| `python main.py sync-inventory` | `ModuleNotFoundError: tenacity` (system Python) | Environment, not code; works in `.venv` until credentials are needed |
| `python main.py orders place --order-id 1 --sandbox-first` | Dry-run printed, then `PermissionError` without `--confirm` | Misleading UX (P0-6) |

### 3.3 Preview server

Started with `LOCAL_API_KEY=testkey`, `LOCAL_BIND_PORT=8123`.

| Request | Result |
|---|---|
| `GET /health` without key | 401 |
| `GET /health` with `X-API-Key: testkey` | 200 `{"ok":true}` |
| `GET /orders/1` with a seeded order | 200, HTML rendered |
| `GET /api/orders/1` | 200, JSON returned |

Started from `%TEMP%` with `PYTHONPATH` pointing at the repository:

| Request | Result |
|---|---|
| `GET /orders/1` | **500**, server log: `jinja2.exceptions.TemplateNotFound: 'order.html' not found in search path: 'preview/templates'` |

### 3.4 Webhook server

Started on port 8124 with **no credentials configured**.

| Request | Result |
|---|---|
| `POST /webhooks/cj` with no `sign` header | 401 |
| `POST /webhooks/cj` with `sign = base64(HMAC-SHA256(key="", body))` | **200** |
| Same valid body sent twice | Second response reports duplicate (when the DB is healthy) |

Port configuration test: started with `WEBHOOK_BIND_PORT=8125` and no `--port`. The server bound to **8001**; 8125 was closed. `cfg.webhook_bind_port` is never read.

### 3.5 Targeted function tests

**Pricing** (`compute_sell_price`, margin 0.35, freight 0):

| Input | Output |
|---|---|
| 1.00 | 1.99 |
| 3.33 | 4.99 |
| 10.00 | 13.99 |
| 25.00 | 33.99 |
| 100.00 | 135.99 |

**Audit chain concurrency** (isolated temp DB, 8 threads × 10 appends):

```
rows: 80
chain valid: False
```

**Inventory join** (isolated temp DB, two variants, two mappings):

```
('shopify', 'V-A') channel_sku=SKU-A -> supplier_sku=SKU-A
('shopify', 'V-B') channel_sku=SKU-B -> supplier_sku=SKU-A
paired rows: 4
```

**Normalizer robustness**:

```
empty price -> EXC InvalidOperation [<class 'decimal.ConversionSyntax'>]
bad stock   -> EXC ValueError invalid literal for int() with base 10: 'abc'
xss         -> 1.0 '<script>alert(1)</script>'
```

**Margin guard and sandbox dry run**: seeded a WooCommerce order (total 42.00, line unit price 21.00 × 2) and a mapped supplier SKU.

```
sandbox: {'dry_run': True, 'payload': {... 'isSandbox': 1,
  'products': [{'vid': 'VID1', 'quantity': '2', 'shippingName': 'SKU1'}]}}
WARNING services.order_service :: margin guard: sell=42.00 cost+freight=42.00
```

After changing the order total to 1.00:

```
WARNING services.order_service :: margin guard: sell=1.00 cost+freight=42.00
sandbox: {'dry_run': True, 'payload': { ... }}
```

No exception was raised in either case. The payload was still produced.

**Orphan PENDING record**: `orders place --order-id 999 --confirm-cj-sandbox` (order does not exist) left:

```
[{'local_order_id': 999, 'cj_order_number': 'LOCAL-999', 'status': 'PENDING'}]
```

---

## 4. Findings and required corrections

The detailed change specifications are in `docs/MODIFICATION-PLAN.md`; this section records the observed behavior and the revision each finding requires.

### F-01 (P0) Inventory variant/SKU mismatch

- **Evidence:** `V-B` resolved to `supplier_sku=SKU-A`; see test above.
- **Cause:** `services/inventory_sync.py:38-45` joins `mapping` on `canonical_sku` only; `channel_sku` is never compared, so the dedup keeps an arbitrary mapping row.
- **Correction:** Join on the variant-level SKU, enforce one mapping per channel variant, and report unmatched rows.

### F-02 (P0) Margin guard is ineffective

- **Evidence:** sell 1.00 vs cost 42.00 produced a warning and a payload; `cost_total` is derived from `order_items.unit_price`, which is the channel selling price.
- **Cause:** `services/order_service.py:275-284`; supplier cost is not persisted anywhere.
- **Correction:** Persist supplier costs during sync, compute cost from the mapped supplier SKU, and raise (not warn) on non-positive margin. Add an explicit, audited override.

### F-03 (P0) Webhook verification accepts forged requests with empty secrets

- **Evidence:** empty-key HMAC returned 200.
- **Cause:** `utils/security.py:8-22` does not reject empty secrets; `webhook/receiver.py` never validates configuration at startup.
- **Correction:** Fail closed on missing secrets both at startup and inside each verifier. Add a negative test.

### F-04 (P0) Audit chain breaks under concurrency

- **Evidence:** 80 concurrent entries, `verify_audit_chain()` false.
- **Cause:** read-last-hash and insert are separate transactions; no serialization or busy timeout.
- **Correction:** Single-writer lock plus `BEGIN IMMEDIATE`, WAL, `busy_timeout`, and a concurrency regression test.

### F-05 (P0) Price rounding destroys the margin model at low prices

- **Evidence:** 1.00 → 1.99 (99% markup instead of 35%).
- **Cause:** `services/pricing.py:18-24` always rounds up to the next `.99`.
- **Correction:** Define a nearest-`.99` or price-band policy, add a maximum-markup sanity check, and table-test the function.

### F-06 (P0) Sandbox path and CJ payload are not trustworthy

- **Evidence:** payload contains `shippingName: "SKU1"` and `isSandbox: 1`; `sandbox` is unused by `CJClient.create_order`; `--sandbox-first` continues into `place_order`.
- **Cause:** `services/order_service.py:186-229`, `clients/cj_client.py:156-158`, `main.py:50-55`.
- **Correction:** Implement a real sandbox target, verify the CJ schema, use a pure dry-run flag, validate before inserting state, and remove acknowledged placeholders.

### F-07 (P0) Webhook events can be silently dropped and are never processed

- **Evidence (static):** `_record` catches all exceptions and returns `False`; `processed` is never updated; handlers only log.
- **Cause:** `webhook/receiver.py:14-25`.
- **Correction:** Only unique-constraint violations mean "duplicate"; return 5xx for other errors; implement or explicitly disable ingestion; add WooCommerce support or remove unused code.

### F-08 (P0) `listing` bypasses lazy credential loading

- **Evidence:** command fails at import with missing Shopify env.
- **Cause:** `services/listing_service.py:5`.
- **Correction:** Lazy accessor and dependency injection.

### F-09 (P1) Preview templates are CWD-relative

- **Evidence:** 500 with `TemplateNotFound` when started from `%TEMP%`.
- **Cause:** `preview/app.py:15`.
- **Correction:** Resolve `preview/templates` from `__file__`.

### F-10 (P1) `WEBHOOK_BIND_PORT` is dead configuration

- **Evidence:** server bound to 8001 despite `WEBHOOK_BIND_PORT=8125`.
- **Cause:** `main.py:20-22`, `main.py:107`.
- **Correction:** Make the flag default to `cfg.webhook_bind_port`, or remove the setting.

### F-11 (P1) Unvalidated supplier data and unsanitized HTML

- **Evidence:** empty price and malformed stock throw; `<script>` survives normalization.
- **Cause:** `services/normalizer.py:13-18`, `:32`.
- **Correction:** Field-level validation, positive price/non-negative bounded stock, HTML allowlist sanitization, URL allowlisting.

### F-12 (P1) Inventory sync ignores `stale_minutes` and uses deltas

- **Cause:** `services/inventory_sync.py:20-31`, `:92-100`, `:125`.
- **Correction:** Implement the filter or remove it; reconcile absolute quantities and store authoritative timestamps.

### F-13 (P1) WooCommerce tracking is incomplete

- **Cause:** `services/order_service.py:391-400`; hardcoded 17track URL for Shopify.
- **Correction:** Channel-appropriate tracking metadata/status update and a stored carrier URL.

### F-14 (P1) Missing startup and resource controls

- Empty `LOCAL_API_KEY` yields a running server that rejects everything (observed 401).
- No body-size limit, rate limit, or request timeout.
- Debug docs endpoints are disabled (good), and binding defaults to `127.0.0.1` (good).

### F-15 (P1) Plaintext SQLite and missing supply-chain controls

- No SQLCipher/field encryption, no file permission enforcement, no WAL, no retention job.
- `requirements.txt` has no hashes, no lock file, no `pip-audit`, no secret scanning.
- `.env.example` is missing `WEBHOOK_BIND_PORT`, `PRICING_DEFAULT_FREIGHT`, and `DROPSHIP_DB`, and documents unused settings.

Additional static issues are enumerated in `docs/MODIFICATION-PLAN.md` (P1-4, P1-5, P1-8, P1-9, P2-1 through P2-10).

---

## 5. Compliance against `보완사항.txt`

| Supplement requirement | Implemented? | Evidence |
|---|---|---|
| No hardcoded secrets; lazy failure on missing credentials | Mostly | `config.require`, lazy clients; but `listing_service` violates it |
| Webhook raw-body HMAC, timing-safe, all providers | Partial | CJ and Shopify implemented; Woo verifier unused; fail-open with empty secret |
| Bind to loopback; debug endpoints disabled | Yes | `LOCAL_BIND_HOST` default `127.0.0.1`; `docs_url=None` on both apps |
| Local API authentication | Partial | API key required; no startup validation, no rotation, no per-route scope |
| SQLite encryption / PII protection | No | Plaintext `dropship.db`; no permission enforcement |
| Secret/PII log masking | Partial | `PIIRedactionFilter` masks key names and email/phone, but not addresses; key-name replacement can corrupt messages |
| Hash-chained audit log | Partial | Implemented but forks under concurrency |
| Dependency hashing and audit | No | No hashes, no lock file, no `pip-audit` |
| Data retention / DPA / card-data exclusion | Partial | Card data is not stored (good); no TTL or retention process |
| Manual approval gate for money actions | Partial | `--confirm` required, but dry-run semantics are broken and the margin guard does not block |
| No retry of uncertain writes | Partial | Deterministic order number helps, but retry semantics after a failed response are not fully verified |

---

## 6. What could not be verified

- Live CJ authentication, product listing, freight calculation, order creation, and tracking.
- Live Shopify GraphQL product creation, publications, fulfillment, and the `nextQueryCost` throttle behavior.
- Live WooCommerce product/variation creation, order collection, review pagination, and comment replies.
- Webhook delivery formats from the real providers (the CJ `type`/`topic` field extraction is unverified).
- Real `.99` policy acceptance by the business owner (the report describes the observed behavior; the final pricing rule is a product decision).

These require credentials and merchant test stores. The dry-run paths, signature verification, SQL behavior, and local HTTP behavior were fully exercised as shown above.

---

## 7. Cleanup performed

- Background preview and webhook servers were stopped.
- Test databases (`dropship.db`, temporary audit/inventory DBs) were removed.
- `.venv` was retained for reproducibility and is ignored by Git.
- `git status` is clean; only the two documentation files were added.

---

## 8. Conclusion

The project is a promising skeleton, but it is **not safe for live orders** in its current state. The three defects that must be fixed first are the inventory mapping join, the non-functional margin guard (including supplier cost persistence), and the fail-open webhook verification. After those, the audit-chain concurrency fix and a real sandbox/dry-run contract are required before any money-moving command is used.

The revised implementation should add regression tests for each P0 item; without them, the same failure modes can reappear silently. The companion document `docs/MODIFICATION-PLAN.md` contains the prioritized change list and acceptance criteria.

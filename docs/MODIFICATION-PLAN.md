# Drop-Shipping Local Application — Modification Plan (English)

Source reviewed: `main.py`, `config.py`, `db.py`, `models.py`, `clients/*`, `services/*`, `utils/*`, `webhook/*`, `preview/*`, plus the design/supplement documents in the repository root.

Scope of this document: the changes required to make the implementation safe and correct before it is used with live orders. Execution evidence is in `docs/RUN-REPORT.md`.

Priority meanings:
- **P0** — money loss, data corruption, or a security fail-open. Fix before any live use.
- **P1** — functional defects, operational fragility, or missing controls. Fix before production rollout.
- **P2** — maintainability, documentation, and hardening.

---

## P0 — Blocking defects

### P0-1. Inventory sync pairs a channel variant with the wrong supplier SKU

**Location:** `services/inventory_sync.py:34-56`

**Problem:** The SQL join links `channel_variants` to `mapping` only on `canonical_sku`. A product has one mapping row per variant SKU, so every channel variant is joined to every mapping row (cartesian product). The deduplication step keeps the first row per `(channel, channel_variant_id)`, ordered only by `m.priority`. It never verifies that `channel_variants.channel_sku` matches `mapping.supplier_sku`.

**Impact:** Inventory for variant B can be overwritten with the stock of variant A. This produces wrong stock on the storefront (overselling or false "sold out"), and it is silent.

**Required change:**
1. Add `AND cv.channel_sku = m.supplier_sku` to the join (or a dedicated variant-level mapping table keyed by channel variant ID).
2. Keep the priority rule only for genuine multi-supplier alternatives of the same SKU.
3. Add a unique constraint or explicit validation so one channel variant cannot map to multiple supplier SKUs.
4. Log and count unmatched variants instead of silently ignoring them.

**Acceptance criteria:** A unit test with two variants (`SKU-A`, `SKU-B`) resolves each to its own supplier SKU: `V-A -> SKU-A`, `V-B -> SKU-B`. Unmatched variants appear in the summary.

---

### P0-2. The margin guard does not guard margin

**Location:** `services/order_service.py:275-284`, `services/order_service.py:200-207`

**Problem:** Two independent defects:
1. `cost_total` is computed from `order_items.unit_price`, but that column stores the **channel selling price** (`collect_woo`/`collect_shopify` write `li.price` / `originalUnitPriceSet`), not the supplier cost.
2. Even when `sell_total <= cost + freight`, the code only calls `log.warning`. The documented behavior ("block the order") is not implemented.

**Impact:** Orders can be placed below cost, and the operator receives only a warning line. CJ supplier prices are never persisted anywhere, so a correct margin cannot be computed from the database at all.

**Required change:**
1. Persist the supplier cost per supplier SKU (add `cost`, `currency`, `cost_updated_at` to `supplier_products`, or write `cost_ledger` entries during product sync).
2. Compute `cost_total` from the mapped supplier cost multiplied by quantity, not from the channel sale price.
3. Make the guard raise a typed exception and abort `place_order` unless an explicit override flag is supplied.
4. Include freight, payment fees, and currency conversion in the calculation, or clearly document that they are excluded.
5. Record the computed margin in the audit log for every placement.

**Acceptance criteria:** `sell_total = 1.00`, supplier cost = `42.00` produces a blocked order with a clear error. The dry-run report shows sell price, supplier cost, freight, and margin per line item.

---

### P0-3. Price rounding overprices low-cost products

**Location:** `services/pricing.py:18-24`

**Problem:** The `.99` rule always rounds the margin-adjusted amount up to the next integer minus `0.01`. For low prices this adds almost a full currency unit, far beyond the configured margin.

**Observed with `PRICING_MARGIN=0.35`:**

| Supplier price | Expected (approx.) | Actual |
|---|---|---|
| 1.00 | 1.35 | **1.99** |
| 10.00 | 13.50 | **13.99** |
| 25.00 | 33.75 | **33.99** |
| 100.00 | 135.00 | 135.99 |

**Impact:** Small-ticket items are priced at up to 99% effective markup. The store becomes uncompetitive, and the pricing rule no longer matches the documented margin.

**Required change:** Choose and document one policy:
- nearest `.99` (round to nearest integer, then subtract `0.01`), or
- a minimum price threshold below which normal 2-decimal rounding is used, or
- round up only after adding a configurable minimum absolute margin.

Also add a maximum-markup sanity check (for example, reject a price more than 2x the margin target for low-cost items) and unit tests for at least 0.50, 1.00, 9.99, 10.00, 25.00, 100.00.

**Acceptance criteria:** The documented margin applies within a stated tolerance for all price bands, and the result is always `>= cost`.

---

### P0-4. Webhook signature verification fails open when secrets are missing

**Location:** `utils/security.py:8-14`, `utils/security.py:17-22`, `webhook/receiver.py:28-33`, `config.py:64-76`

**Problem:** The webhook server never calls `require("cj")` or validates that the signing secrets are configured. If `CJ_OPEN_ID` is empty, `verify_cj` computes HMAC-SHA256 with an empty key. An attacker knows that key and can generate a valid `sign` header for any body. The same applies to `SHOPIFY_CLIENT_SECRET` in `verify_shopify`.

**Observed:** With no credentials configured, a request signed with HMAC key `b""` returned HTTP 200. An unsigned request returned 401.

**Impact:** In a misconfigured deployment, anybody on the tunnel can inject fake order, stock, or shipment events. This is exactly the fail-open pattern the supplement document forbids.

**Required change:**
1. At application startup (lifespan handler), refuse to start if the required webhook secrets are empty.
2. Inside `verify_cj` / `verify_shopify` / `verify_woo`, return `False` immediately when the secret is empty (defense in depth).
3. Add a startup log line that states which providers are enabled, without printing secrets.
4. Keep raw-body verification before JSON parsing (already correct).

**Acceptance criteria:** Starting the webhook server without `CJ_OPEN_ID` fails fast with a clear error. With a secret configured, an empty-key signature is rejected with 401.

---

### P0-5. Audit hash chain is not safe under concurrency

**Location:** `utils/audit.py:11-16`, `utils/audit.py:36-48`, `db.py:169-177`

**Problem:** `_last_hash()` and the following `INSERT` are two separate transactions. Under concurrent writers, multiple entries read the same predecessor and fork the chain. There is no `BEGIN IMMEDIATE`, no application lock, and no `busy_timeout`/WAL configuration.

**Observed:** 80 entries written by 8 threads produced `verify_audit_chain() == False`.

**Impact:** A legitimate concurrent run breaks tamper evidence, and a tampered chain cannot be distinguished from a concurrency artifact.

**Required change:**
1. Serialize append with an application-level `threading.Lock` and a database transaction using `BEGIN IMMEDIATE` (single-process scope).
2. Enable WAL and set a non-zero `busy_timeout` in `get_conn`.
3. For multi-process use, move audit appends behind a single writer or use an OS file lock.
4. Store a canonical timestamp in the hash payload and re-use exactly the stored value during verification.
5. Add a concurrency test that writes at least 100 entries from multiple threads and asserts a valid chain.

**Acceptance criteria:** The concurrency test above always verifies `True`.

---

### P0-6. Sandbox and dry-run semantics are inconsistent and unverified

**Location:** `main.py:50-55`, `clients/cj_client.py:156-158`, `services/order_service.py:186-229`

**Problems:**
1. `CJClient.create_order(..., sandbox=...)` accepts `sandbox` but never uses it. There is no sandbox base URL switch.
2. The payload contains `"isSandbox": is_sandbox`, marked in the code itself as unverified; the CJ API may ignore or reject it.
3. `shippingName` is set to the supplier SKU, while the code comment states it is a logistics option name. This is an acknowledged placeholder that can produce invalid orders.
4. `orders place --sandbox-first` performs a dry run and then unconditionally calls `place_order`. Without `--confirm` it raises `PermissionError`; it is not a standalone dry run as the help text implies.
5. The PENDING `cj_orders` row is inserted before the order is validated. An invalid order ID leaves an orphan PENDING row.

**Required change:**
1. Implement a real sandbox endpoint (`CJ_BASE` sandbox host) or remove the misleading parameter.
2. Confirm the CJ create-order schema against the current official documentation. Remove `shippingName` unless a logistics option name is genuinely required, and replace `isSandbox` with the documented mechanism.
3. Make `--sandbox-first` strictly a dry run that prints the payload and exits; require an explicit separate `--confirm` for live placement.
4. Validate the order and build the payload **before** inserting the PENDING row; wrap the insert and validation in one transaction.
5. Add a `--dry-run` JSON output test for a mapped order.

**Acceptance criteria:** Dry run performs no network call and makes no database writes. A live placement cannot happen without `--confirm`. Invalid order IDs leave no rows behind.

---

### P0-7. Webhook deduplication fails open and events are never processed

**Location:** `webhook/receiver.py:14-25`, `webhook/receiver.py:41-46`, `db.py:118-126`

**Problem:** `_record` catches **every** exception and returns `False`, which the handlers interpret as "duplicate". A transient database error therefore causes a real event to be acknowledged and discarded. Additionally, `webhook_events.processed` is never set and no worker consumes the events; the handlers only write a log line.

**Impact:** Silent event loss under DB pressure, and the system appears to support webhooks while doing nothing with them.

**Required change:**
1. Catch only the unique-constraint violation and return "duplicate". Re-raise all other errors so the provider retries.
2. Mark `processed` after successful handling, and add a worker or clearly return 501/503 if ingestion is not implemented.
3. Store a bounded, sanitized payload (or a digest) with a retention policy.
4. Add a WooCommerce endpoint (`X-WC-Webhook-Signature`) or remove the unused `verify_woo`.

**Acceptance criteria:** A forced DB error returns 5xx (provider retries) rather than `{"ok": true}`.

---

### P0-8. `listing_service` instantiates the Shopify client at import time

**Location:** `services/listing_service.py:5`

**Problem:** `shopify = shopify_client.ShopifyClient()` runs on import, bypassing the deliberate lazy-credential design (`P2-23`) used elsewhere. Any `listing` command, including `--help`-adjacent validation, fails before argument handling when Shopify credentials are absent.

**Observed:** `python main.py listing publish --sku TEST-1` raised `RuntimeError: Missing required env — shopify: ...` from the module import, not from the command logic.

**Required change:** Replace the module-level instance with a lazy accessor identical to `_shopify()` in the other services, and inject the client into `set_listing` for testability.

**Acceptance criteria:** Importing `services.listing_service` succeeds with an empty environment; credentials are validated only when a Shopify call is actually made.

---

## P1 — Functional and operational defects

### P1-1. Preview template path depends on the current working directory
**Location:** `preview/app.py:15`
`Jinja2Templates(directory="preview/templates")` is relative. Running the app from any directory other than the repository root returns HTTP 500 (`TemplateNotFound`). Observed from `%TEMP%`.
**Change:** Resolve the directory from `Path(__file__).parent / "templates"`.
**Acceptance:** `/orders/{id}` renders with HTTP 200 from any working directory.

### P1-2. `WEBHOOK_BIND_PORT` is ignored
**Location:** `main.py:20-22`, `main.py:107`, `config.py:37`
The webhook command uses `args.port` (default 8001) and never consults `cfg.webhook_bind_port`. Observed: with `WEBHOOK_BIND_PORT=8125`, the server still bound to 8001.
**Change:** Use the config value as the default for `--port`, or document that only the flag is honored and remove the unused setting.

### P1-3. Supplier input is not validated and HTML is not sanitized
**Location:** `services/normalizer.py:13-18`, `services/normalizer.py:32`, `clients/shopify_client.py:204`, `clients/woo_client.py:46`
- An empty `variantSellPrice` raises `decimal.InvalidOperation`; a non-numeric `variantInventory` raises `ValueError`. One malformed variant aborts the whole product sync.
- `description` is copied into `descriptionHtml`/`description` without sanitization. Observed: `<script>alert(1)</script>` is preserved verbatim.
- Price and stock have no range validation (negative values are accepted).
**Change:** Validate per field, skip/flag bad variants instead of aborting the product, enforce non-negative bounded stock and positive price, sanitize HTML with an allowlist, and reject external image URLs that are not allowlisted (SSRF policy).

### P1-4. Shopify option handling silently truncates variant values
**Location:** `clients/shopify_client.py:210-216`, `clients/shopify_client.py:233-237`
`o["values"][:3]` drops every option value after the third. Bulk variant creation will then fail for those variants, or create an incomplete product.
**Change:** Reject products with more than the supported number of values per option with an explicit error, or split/paginate the option creation correctly. Never truncate silently.

### P1-5. Shopify retry and throttling behavior loses server guidance
**Location:** `clients/shopify_client.py:49-81`
After five 429 responses the client raises `ShopifyThrottled(2)` with a hardcoded value instead of the last `Retry-After`. Cost estimation starts at 10 and the budget counter is process-local.
**Change:** Re-raise with the actual `Retry-After`, use the GraphQL `throttleStatus` for backoff, and add jitter. Consider a shared limiter if more than one process runs.

### P1-6. Inventory sync ignores its stale window and uses delta updates
**Location:** `services/inventory_sync.py:20-31`, `services/inventory_sync.py:92-100`, `services/inventory_sync.py:125`
`stale_minutes` is computed but never used to filter rows. Shopify stock is adjusted by `new_qty - last_pushed_qty`; if stock was changed in the channel manually, the mirror drifts permanently.
**Change:** Either implement the stale filter or remove the parameter. Prefer absolute reconciliation (`inventorySetQuantities` or read-current-then-adjust with the real current quantity) and store the authoritative quantity with a timestamp.

### P1-7. Tracking push does not update the channel for WooCommerce
**Location:** `services/order_service.py:391-400`
For WooCommerce, only an order note is added. The order status is not advanced to a shipped/completed state and no tracking metadata is attached. For Shopify, the tracking URL is a hardcoded 17track URL rather than the carrier URL.
**Change:** Add a tracking integration appropriate to the channel (Woo shipment tracking plugin/meta or status transition), store the carrier tracking URL in `cj_orders`, and use it for both channels.

### P1-8. Country and address validation is incomplete
**Location:** `services/order_service.py:169-183`
WooCommerce accepts an empty country (only values longer than two characters are rejected). Required CJ fields (province, city, address, postal code, phone) are not validated before the API call.
**Change:** Require a two-letter ISO country, validate all mandatory CJ fields, and fail the dry run with a field-level report. Normalize names/phone formats per destination.

### P1-9. Review synchronization reads only the first page
**Location:** `clients/woo_client.py:221-234`
`list_reviews` requests one page of up to 100 reviews and never paginates. Older reviews are never refreshed, and new reviews beyond page one are missed on large stores.
**Change:** Iterate pages using `X-WP-TotalPages`, mirroring `iter_all_orders`, and store `date_created_gmt`.

### P1-10. Local API and webhook resource limits are missing
**Location:** `webhook/receiver.py`, `preview/app.py`
No request body size limit, no rate limit, no concurrency limit, and no timeout discipline. A local service exposed through a tunnel can be exhausted by a large or repeated body.
**Change:** Enforce a maximum body size before JSON parsing, add per-IP rate limiting, set request timeouts, and keep `/health` unauthenticated only if it reveals nothing sensitive.

### P1-11. Local API key is not validated at startup
**Location:** `utils/security.py:40-42`, `preview/app.py:9-14`
If `LOCAL_API_KEY` is empty, the preview server starts and rejects every request. This is fail-closed but operationally opaque: the failure looks like a wrong key rather than a configuration error.
**Change:** Validate `LOCAL_API_KEY` during startup and exit with an actionable message. Consider hashing the key at rest and supporting rotation.

### P1-12. Database hardening is absent
**Location:** `db.py:160-177`
The SQLite file is plaintext, default file permissions apply, WAL is not enabled, and `busy_timeout` is zero. Order, address, email, and review data are stored unencrypted, contrary to the security supplement.
**Change:** Apply SQLCipher or field-level encryption for PII, set `0600` permissions on the DB/WAL/SHM files, enable WAL and `busy_timeout`, and define a retention/TTL job.

### P1-13. Dependency supply chain controls are absent
**Location:** `requirements.txt`
Versions are pinned but not hashed; there is no `--require-hashes`, no `pip-audit`, no lock file, and no index restriction.
**Change:** Add hashes and a lock file, run `pip-audit` in CI/pre-commit, add `gitleaks`, and document the supported Python range. The current header says 3.14/3.15 are unverified while the test machine runs 3.15.

### P1-14. Configuration documentation is out of date
**Location:** `.env.example`
Missing `WEBHOOK_BIND_PORT`, `PRICING_DEFAULT_FREIGHT`, and `DROPSHIP_DB`. Several settings are unused (`PRICING_CURRENCY`, `CJ_SANDBOX`, `webhook_bind_port`), which misleads operators.
**Change:** Generate `.env.example` from the config schema, document each value, and remove or implement unused settings.

---

## P2 — Maintainability and polish

1. `main.py:168-171` calls `init_db()` before parsing arguments; `--help` mutates the filesystem. Move initialization after parsing and into the command path.
2. `utils/logging_setup.py:19` replaces a key name with `key=***`, which alters messages such as `token expired` into `token=*** expired`. Redact only value positions and extend masking to addresses/names.
3. `Config` reads the environment at import time while `require()` re-reads `os.environ`; cache and validate once, or read both from the same snapshot.
4. `cost_ledger` is defined but never written. Either use it for P0-2 or remove it.
5. Shopify product images are never uploaded; `NormalizedProduct.images` is ignored on Shopify.
6. Shopify reviews return `NotImplementedError`; expose the limitation in the CLI help and the operator UI.
7. `_extract_cj_order_id` accepts `orderNumber` as an order ID. If CJ returns only a merchant-facing number, subsequent `query_order` calls will fail. Validate against a recorded response fixture.
8. `order_items` deletion and re-insertion on every collection discards any future local annotations. Preserve item rows by `(order_id, channel_line_id)` with an upsert.
9. Add a test suite: pricing tables, payload builder, signature verification, inventory join, audit concurrency, and a mocked end-to-end sync. None exists today.
10. Add structured JSON logs with a request ID propagated into the audit log.

---

## Recommended implementation order

1. P0-1, P0-2, P0-3 (inventory and money correctness).
2. P0-4, P0-7 (webhook fail-open).
3. P0-5 (audit integrity), P0-6 (sandbox/payload correctness).
4. P0-8, then P1-1/P1-2/P1-11 (operational startup behavior).
5. P1-3, P1-4, P1-6, P1-7 (channel synchronization correctness).
6. P1-12, P1-13 (security and supply chain).
7. Remaining P1/P2 items with tests.

## Definition of done

- Every P0 item has an automated regression test that fails on the current code.
- Dry-run and webhook paths require no live credentials, and live paths fail closed without them.
- A full order can be collected, priced, previewed, placed in sandbox, and tracked using mocked APIs.
- `pip-audit`, `gitleaks`, and the test suite run in CI.
- README/operator runbook documents the supported Python version, credential setup, tunnel binding, and the manual approval gate for money-moving actions.

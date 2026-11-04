# Codemap — polaris-ea-fork (Easy!Appointments fork for Phase 04)

**Last Updated:** 2026-06-24 | Image **v1.6.0-r18** built+pushed to GHCR (`ghcr.io/jiun01/polaris-ea:v1.6.0-r18`), pinned in `infra/docker-compose.prod.yml` (DEPLOYED v1.0.67).
**r18 (2026-06-24):** booking-confirmation loop — `Booking::register()` fires HMAC-signed `{polaris_ref,appointment_id,start,end,service}` POST to `{POLARIS_API_BASE_URL}/v1/webhooks/ea` on save (provider-tz ISO offset → no skew). New env var `POLARIS_API_BASE_URL` read via `application/config/polaris.php` → `polaris_api_base_url`. `EA_ASSET_TOKEN` build-arg now stamped into `app.php` `cache_busting_token` at build time via `infra/docker-bake.hcl` (auto cache-busts all front-end assets per image — no more manual `?TSJ85` stale cache).
**r7 (2026-06-20) — UI REDESIGN (booking surface full, user-verified; calendar-admin first pass):** front-end-only restructure to a modern Cal-style look (CSS/PHP/JS; no backend/API change). New `assets/css/_tokens.scss` = `--ea-*` neutral palette/radii/elevation/motion (AA-corrected contrast), imported via `general.scss`. Booking: card shell; **two-panel date picker** — flatpickr restyled into a Cal-style month grid (rounded-square grey day cells, near-black selected day+slot, today dot, faded past, borderless) with month as plain non-selectable text via `monthSelectorType:'static'` (`assets/js/utils/ui.js`); slot pill grid; tokenized form controls/buttons; step-4 confirmation summary cards. Kept **tenant-coloured header** + **Bricolage Grotesque** brand font (already vendored). Admin (`backend.scss`): token radius/elevation on buttons/forms/cards/modals + rounded event chips; removed About-page "Latest Blog Posts" + "License" sections + footer "Licensed Under GPL-3.0". a11y: removed `user-scalable=no` (booking + backend layouts), global `:focus-visible` + `prefers-reduced-motion`. Built EA-only via `ship.sh --no-app`. Branch `ea-redesign/safe-foundation`. **Gotcha:** local EA serves `.min.css`/`.min.js` (prod mode) with a static `?TSJ85` cache-buster → hard-refresh to see asset changes; `gulp build` errors on `archive` step → use `gulp scripts styles`.
**r6 (2026-06-18):** prod `intl`/`gd` fix in the **top-level `Dockerfile`** (built by `--target production`), not just the dev `docker/php-fpm/Dockerfile`.
**r5 (2026-06-14):** (1) `application/config/routes.php` += `$route['admin'] = 'login'` — EA 1.6 has no `/admin` controller; alias routes `/admin` → Login → /calendar so "Open Calendar admin" links work. (2) `docker/php-fpm/Dockerfile` pinned PHP 8.4, dropped `apt-get autoremove` (it was stripping `libicu`/`libpng` → `intl`/`gd` extensions fail-to-load → 502), added build-time extension-load assert. Pinned in `infra/docker-compose.prod.yml` as `v1.6.0-r5`.
**r3 (2026-06-01):** API-contract fix: `Appointments_api_v1::store()` accepts Polaris booking-node contract (high-level `service_id`/`provider_id` + nested `customer{}` + ISO→`Y-m-d H:i:s`) by skipping `api_decode` for Polaris payloads. `Availabilities_api_v1` + appointments controllers also accept snake_case. Verified live.
**Earlier:** 2026-05-29 | Rate-limiting fix (booking_register namespaced key); Phase 20 complete | v2.1 (PR #68): Polaris-side EA→Polaris appointment webhook (`POST /v1/webhooks/ea`) merged — fork emitter **COMPLETE in r18** (see v2.1 section below)

The EA fork is a whitelabeled Easy!Appointments v1.6 instance that provides the public booking UI for Polaris tenants. Runs at `http://localhost:8090` in local dev (Docker Compose). Phase 04 adds E2E smoke tests that verify booking flow end-to-end + no Easy!Appointments branding leak.

## Key Entry Points

| Path | Purpose |
|---|---|
| `application/controllers/Booking.php::slug($slug)` | Tenant slug resolver; validates format, looks up `ea_polaris_tenants` table, sets CI session `booking_tenant_id` |
| `application/controllers/Booking.php::index()` | Render booking page with tenant-specific company name/logo/color |
| `application/controllers/Booking.php::register()` | Submit booking; **rate-limited to 10/hr per IP** with namespaced cache key (Phase 04 fix) |
| `application/controllers/EA_Controller.php::__construct()` | Parent rate limit: 100 requests per 120s (shared across all EA routes); **DO NOT increment for booking register** |

## Phase 04: Rate-Limiting Fix (2026-05-28)

**Bug:** `register()` called `rate_limit($ip, 10, 3600)` which used default cache key format, colliding with `EA_Controller::__construct`'s `rate_limit($ip)` call (max=100, 120s). All AJAX calls during booking wizard (getUnavailableDates, getAvailableHours, etc.) incremented the shared counter, causing premature 429s in E2E test runs.

**Fix:** Namespaced the cache key in `register()` to `'booking_register_' . $ip`. Now only actual booking submissions count against 10/hr limit.

```php
// application/controllers/Booking.php (Booking::register)
rate_limit('booking_register_' . $this->input->ip_address(), 10, 3600);
```

**Impact:** E2E test cleanup function (`cleanupEATestData()` in admin global-setup.ts) now properly clears rate-limit files before each run:
```bash
docker exec polaris-ea-fork-php-fpm-1 rm -f /var/www/html/storage/cache/rate_limit_key_* /var/www/html/storage/cache/rate_limit_tmp_*
```

## Data Model (Polaris Integration)

### ea_polaris_tenants (Custom table)

Maps public booking slugs to Polaris tenant ids. PK is `tenant_id`. Populated via `POST /api/v1/internal/tenants/sync` (HMAC-signed, `Internal_api_v1`):

| Column | Type | Purpose |
|---|---|---|
| `tenant_id` | VARCHAR(36) | PK; Polaris `tenants.id` (UUID string) |
| `slug` | VARCHAR(100) | URL-safe slug for `/book/{slug}`; unique index |
| `status` | ENUM('active','suspended') | Active tenants only are routable |
| `synced_at` | DATETIME | Last sync timestamp (UTC, TZ-neutral) |

## Whitelabel Verification

All booking pages must NOT contain:
- "Easy!Appointments" (case-insensitive, with/without `!`)
- "easyappointments.org"
- Easy!Appointments meta tags (especially `<meta name="generator">`)

AC-04-1 through AC-04-10 check:
- Page title
- Body text (`document.body.innerText`)
- Meta tags (`<meta content>`)
- 404 pages (when slug not found)
- Generator tag (should be absent or Polaris-branded)

## AJAX Workflow (Booking Wizard)

1. **Step 1: Service/Provider selection** → `getServices()` AJAX
2. **Step 2: Date picker** → `getUnavailableDates()` → `getAvailableHours()` AJAX (triggers on each date click)
3. **Step 3: Time slot selection** → slots populated from step 2
4. **Step 4: Customer details** → no AJAX
5. **Step 5: PDPA consent** → required on slug-routed pages
6. **Step 6: Submit** → `POST /booking/register` (rate-limited, namespaced key)

**Playwright E2E gotchas:**
- `waitForLoadState("networkidle")` before date click to prevent race conditions in `applyUnavailableDates`
- UTC-aware date arithmetic (container timezone = Asia/Kuala_Lumpur, UTC+8)
- Flatpickr calendar rendered as sibling, not child of input
- UTF-8 BOM on JSON responses: strip with `.replace(/^﻿/, "")`

## Admin API (Phase 04)

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /api/v1/appointments` | List appointments | Bearer token (E2E fixture token from .env) |
| `GET /api/v1/providers` | List providers | 401 without auth |

Used in E2E tests for verification (AC-04-4 and AC-04-5).

## Docker Compose (local dev)

The EA fork has its own `docker-compose.yml` in `polaris-ea-fork/`. Running `docker compose up` there with the `docker-compose.override.yml` starts:

```yaml
# polaris-ea-fork/docker-compose.yml (simplified)
php-fpm:          # PHP-FPM; container name polaris-ea-fork-php-fpm-1
  build: docker/php-fpm
  volumes:
    - '.:/var/www/html'            # bind-mount; rate_limit_* under storage/cache/

nginx:            # nginx reverse proxy; container name polaris-ea-fork-nginx-1
  image: nginx:1.27-alpine
  ports: ${NGINX_PORT:-8090}:80
  upstream: php-fpm:9000           # fastcgi_pass php-fpm:9000 in nginx.conf

mysql:            # MySQL 8.0 for EA data
  image: mysql:8.0
```

**Prod image:** `ghcr.io/jiun01/polaris-ea:v1.6.0-r5` (self-configuring; no `config.php` baked in — written from env by `docker/php-fpm/docker-entrypoint.sh` on boot). Prod services are `ea-php-fpm` / `ea-nginx` in `infra/docker-compose.prod.yml`.

**r5 key files:**
- `application/config/routes.php` line ~63–68: `$route['admin'] = 'login'` alias
- `docker/php-fpm/Dockerfile`: PHP 8.4 pin; `apt-get autoremove` removed; extension-load assert added

## v2.1: Branch EA provider/service write-back (auto-provisioned, no typed ids)

`tenant_branches.ea_provider_id` / `ea_default_service_id` are **system-set**, never a hand-typed operator/tenant field — drift would silently misroute bookings to the wrong calendar.

- **Production:** `POST /api/admin/tenants/{id}/ea-providers/sync` (`routers/admin_ea.py`, platform_admin) calls EA `list_providers` + `list_services`, matches provider→branch (single-branch tenant = unambiguous auto-bind; multi-branch = normalized name match; ambiguous = reported, not guessed), then writes the first-class columns via `services/booking/provisioning.sync_tenant_branch_ea_bindings` → `set_branch_ea_binding`. Idempotent.
- **Seed:** `seed_e2e_ea_providers.py` calls the SAME writer (`set_branch_ea_binding_by_slug`) so seed and prod never drift (previously it wrote only the deprecated `metadata.ea_provider_id` JSONB).
- **Client:** `EABookingClient` gained `list_providers` (`GET /api/v1/providers`, tenant-JWT).
- **Deferred (not built):** an admin-only dropdown populated from `list_providers`/`list_services` — never a free-text integer.

## Tenant Sync (Polaris → EA)

The EA fork's `ea_polaris_tenants` shadow table is kept current via an inbound HMAC-signed endpoint **already built in the fork**:

- **Fork endpoint:** `POST /api/v1/internal/tenants/sync` (`Internal_api_v1::tenants_sync()`); auth = `X-Polaris-Signature: sha256=<hmac>` using `POLARIS_WEBHOOK_SECRET` / `EA_WEBHOOK_HMAC_SECRET`.
- **Body:** `{ "tenant_id": "<uuid>", "slug": "<slug>", "status": "active|suspended" }` — upserts the `ea_polaris_tenants` row idempotently.
- **Polaris side:** `routers/` EA internal router calls this on tenant create/update/suspend events.

## v2.1: EA → Polaris appointment webhook (✅ COMPLETE — EA r18 / v1.0.67)

Polaris confirms link-flow bookings via an inbound webhook. The bot sends the customer an EA link carrying `?polaris_ref=<signed HMAC token>`; the EA fork captures the ref and **POSTs it back** after a successful booking so Polaris can bind the EA appointment to the verified chat identity.

**Implementation (BUILT in r18):**

EA side (`application/controllers/Booking.php`):
- `index()` — extracts `?polaris_ref=` from the URL query string, sanitizes (rejects malformed tokens), stores it as a PHP script_var on the page
- `assets/js/http/booking_http_client.js` — attaches `polaris_ref` from the script_var to the `/booking/register` JSON POST body
- `register()` — after a successful appointment insert, calls `notify_polaris_appointment_confirmed()` when `polaris_ref !== ''`
- `notify_polaris_appointment_confirmed($appointment, $service, $provider, $polaris_ref)` — new private method:
  - Builds body: `{ "polaris_ref": "...", "appointment_id": <int>, "start_datetime": "<provider-tz ISO8601 offset>", "end_datetime": "<...>", "service_name": "<...>" }`
  - Signs with HMAC-SHA256 using `polaris_api_base_url` config key (env `POLARIS_API_BASE_URL`)
  - Posts to `{polaris_api_base_url}/v1/webhooks/ea` with header `X-Polaris-Signature: sha256=<hex>`
  - Best-effort only — exceptions logged, never abort the booking response

New config key (`application/config/polaris.php`):
- `polaris_api_base_url` = `getenv('POLARIS_API_BASE_URL') ?: 'https://api.getpolaris.my'`

Polaris receiver (`apps/api/polaris_api/webhooks/ea_router.py` — was already present since PR #68):
- `POST /v1/webhooks/ea` — HMAC-verify `X-Polaris-Signature` using `ea_webhook_hmac_secret`
- `verify_binding_token(polaris_ref)` → `link_flow.confirm_booking(...)` — idempotent; 401 on bad HMAC, 400 on unknown ref

Asset auto cache-bust (`Dockerfile` ARG, `infra/docker-bake.hcl`):
- `ARG EA_ASSET_TOKEN=""` in the prod Dockerfile; when non-empty, `sed` stamps `app.php` `cache_busting_token` with the image tag
- `docker-bake.hcl` ea target: `args = { EA_ASSET_TOKEN = "${EA_TAG}" }` — every ship now busts all `.min.js`/`.min.css` URLs automatically
- **Gotcha:** prod serves MINIFIED assets; if `gulp compile` layer is buildx-cached the new JS won't be served → always pass `--no-cache` to bake after JS source changes

## Related Areas

- [admin.md — E2E test infrastructure (global-setup, phase-04-smoke.spec.ts)](admin.md)
- [backend.md — Webhook handlers & tenant sync (EA internal router)](backend.md)
- Phase-04 checklist: AC-04-1 through AC-04-10 acceptance criteria

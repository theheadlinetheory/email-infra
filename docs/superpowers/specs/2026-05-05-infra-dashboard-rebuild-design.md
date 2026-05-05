# Infra Dashboard Rebuild — Design Spec

**Date:** 2026-05-05
**Status:** Approved
**Approach:** Clean Frontend + Hardened Backend (Option B)
**North Star:** `~/Desktop/code-quality-review/` — SOLID, DRY/KISS/YAGNI, Composition/Coupling principles. Every module, function, and component must pass these checks.

## Problem

The THT Infrastructure Dashboard is unreliable. External API errors (SmartLead, Zapmail) crash responses instead of degrading gracefully. Data goes stale without indication. Actions (assign, swap, cancel) fail silently. The 3800-line Python monolith tangles routing, business logic, and API calls, making issues hard to diagnose and fix. The frontend uses global variables, innerHTML string concatenation, and has no proper error/loading states.

## Goals

1. **Reliability** — external API failures degrade gracefully instead of crashing. Cached data served when APIs are down. Every action gives clear success/failure feedback.
2. **CRM-quality frontend** — ES modules, reactive state, proper loading/error/data states, shared design language with the CRM (DM Sans, same spacing/color tokens).
3. **Consolidation-ready** — shared Firebase Auth, clean API contract, portable view modules. Merging into the CRM later means importing views and pointing at the API URL.

## Non-Goals

- Rewriting the Python backend in TypeScript/Edge Functions (wrong tool for orchestration)
- Building a shared component library or monorepo (YAGNI)
- Role-based access control (small team, same access level)
- Changing the deployment platform (stays on Render)

## Architecture

### Backend

The Python HTTP server stays but gets restructured from a monolith into proper modules.

```
server/
  app.py              — HTTP server + routing dispatcher (thin layer)
  auth.py             — Firebase token verification
  middleware.py       — request logging, error wrapping, CORS
  routes/
    overview.py       — /api/overview, /api/clients
    client.py         — /api/client/*/accounts, trends, archive
    zapmail.py        — /api/zapmail, /api/wallet, /api/subscriptions
    domains.py        — /api/domains, auto-renew
    pipelines.py      — /api/pipeline/*, /api/setup-pipeline/*
    operations.py     — SSE endpoints (assign, delete, transition)
    acquisition.py    — /api/acquisition, rotation, generic groups
    inventory.py      — /api/domain-inventory, unassigned, untagged
  services/
    smartlead.py      — SmartLead API client with retry + caching
    zapmail_api.py    — Zapmail API client with retry + caching
    registrars.py     — Porkbun + Spaceship clients with retry
    sheets_service.py — Google Sheets wrapper
  cache.py            — TTL cache for API responses (in-memory, configurable)
  errors.py           — Structured error types, error-to-HTTP mapping
```

**Unchanged:** SSE for long-running operations, background threads for sync/pipelines/monitoring, Supabase for persistence (db.py), Render deployment.

### Reliability Layer

**Retry with backoff:** Every external API call retries 3x with exponential backoff. Most SmartLead 500s are transient.

**Response caching:** In-memory TTL cache. Overview data cached 60s, domain data 5min, wallet 10min. Mutations (assign, delete, tag) bust the relevant cache key.

**Graceful degradation:** The overview calls SmartLead, Zapmail, and Supabase. If SmartLead is down, the other sections still return. Failed sections come back as `null` with an error entry.

### API Contract

Every endpoint returns a consistent shape:

```json
{
  "data": { "clients": [...], "acquisition": [...] },
  "errors": [],
  "meta": { "cached": false, "timestamp": "2026-05-05T14:00:00Z" }
}
```

**Partial failure:**
```json
{
  "data": { "clients": [...], "acquisition": null },
  "errors": [{ "section": "acquisition", "code": "TIMEOUT", "message": "SmartLead GQL timed out" }],
  "meta": { "cached": false, "timestamp": "2026-05-05T14:00:00Z" }
}
```

**Cached response:**
```json
{
  "data": { ... },
  "errors": [],
  "meta": { "cached": true, "stale_seconds": 45, "timestamp": "2026-05-05T13:59:15Z" }
}
```

Rules:
- Never crash a response because one external API is down
- Always include `meta.cached` and `meta.timestamp`
- Retry 3x before returning an error
- Mutations bust the relevant cache

### Frontend

Rebuilt from scratch using CRM patterns.

```
js/
  core/
    api.js         — fetch wrapper with auth headers, retry, error normalization
    state.js       — reactive store (subscribe/notify, no globals)
    auth.js        — Firebase Auth init, token management, login/logout
    router.js      — tab/view switching with URL hash routing
    events.js      — pub/sub event bus for cross-module communication
  components/
    toast.js       — notification toasts
    modal.js       — reusable modal with backdrop blur
    stat-card.js   — stat card component
    data-table.js  — sortable table with loading/error/empty states
    sse-progress.js — step-by-step progress display for long ops
  views/
    overview.js    — main dashboard (client cards, health, alerts)
    client-detail.js — single client deep dive (accounts, trends, actions)
    pipelines.js   — pipeline list + step progress
    zapmail.js     — ZapMail domains, mailboxes, renewals
    domains.js     — registrar domains, expiry, auto-renew
    acquisition.js — acquisition groups, rotation, campaigns
    sync.js        — ZapMail ↔ SmartLead sync check
  app.js           — entry point: init auth, mount router, load first view
```

**Key patterns:**
- **Reactive state store** — no global variables. Views subscribe to state slices and re-render when data changes.
- **Three-state rendering** — every view handles loading (skeleton/spinner), error (message + retry button), and data (normal render) explicitly.
- **DOM builder helpers** — like CRM's html-helpers pattern. No innerHTML string concatenation.
- **ES modules** — clean imports/exports, no script tag ordering.

### Auth

Firebase Auth shared with the CRM — same Firebase project (`tht-crm`, ID: `tht-crm`), same Firestore `users` collection for role lookup. One login for both apps.

- Frontend: Firebase JS SDK handles login UI, stores JWT
- Backend: `auth.py` verifies Firebase JWT on every request via `Authorization: Bearer <token>` header
- Replaces the current password-in-URL-param approach
- `/healthz` endpoint remains unauthenticated for Render health checks

### Design System

**Theme:** Light and dark mode via CSS custom properties with user toggle (persisted to localStorage). Both themes share the same design tokens.

**Shared with CRM:**
- Font: DM Sans
- Spacing scale: 4/8/12/16/24/32px
- Color semantics: green (#22c55e) = healthy, amber (#f59e0b) = warning, red (#ef4444) = alert
- Component patterns: compact cards, dense tables, 10-12px body text, sticky topbar
- Transitions: 0.12-0.15s (fast, CRM-matching)

**Stale data indicator:** Sections using cached data show a subtle amber badge "Updated 2m ago" with click-to-refresh. Sections that failed show a red error card with retry button.

## Consolidation Strategy

The architecture is designed so merging into the CRM later is trivial:

1. **Shared Firebase Auth** — same user pool, no migration needed
2. **Clean API contract** — CRM can call the infra API at its Render URL
3. **Shared design tokens** — infra views look native inside CRM shell
4. **Portable ES module views** — each view mounts into any container element. Import and mount in a new CRM tab.

**What consolidation looks like (future):**
- Add "Infrastructure" tab to CRM nav
- Import infra view modules
- Point API calls at Render backend URL
- Views render inside CRM shell

**Explicitly deferred:** No shared component library, no monorepo, no API gateway, no shared deployment pipeline.

## Scope

### In Scope
- Restructure Python backend into modules (routes/, services/)
- Add retry, caching, structured errors to all external API calls
- Implement consistent API response contract
- Rebuild frontend with ES modules, reactive state, three-state rendering
- Firebase Auth integration (shared with CRM)
- Light/dark theme with CRM design tokens
- Stale data indicators and graceful degradation UI
- Render deployment (keep existing)

### Out of Scope
- New features (no new API routes or dashboard functionality)
- Backend language/framework change
- Role-based access control
- CRM consolidation (deferred)
- Mobile responsive design

## Code Quality Standards

All code must satisfy `~/Desktop/code-quality-review/` principles. Every module is reviewed against these before merging.

### SOLID
- **SRP:** Each route file handles one domain (overview, zapmail, domains). Each service wraps one external API. Each view renders one page. No mixing data fetching, business logic, and presentation.
- **OCP:** API service clients accept configuration (base URL, retry count, TTL) so new APIs can be added without modifying existing service code.
- **DIP:** Route handlers receive service instances, not raw `requests.get()` calls. Views call `api.js` abstractions, not `fetch()` directly.

### DRY/KISS/YAGNI
- **DRY:** Retry logic lives in one place (each service's base client). Error-to-HTTP mapping lives in `errors.py`. Frontend three-state rendering is a shared pattern in `api.js`, not reimplemented per view.
- **KISS:** No framework on backend (http.server stays). No build step on frontend (ES modules loaded directly). Max 3 levels of nesting in any function.
- **YAGNI:** No abstractions for single use cases. No config options for hypothetical needs. Dead code deleted, not commented.

### Composition & Coupling
- **Composition:** Views compose from components (stat-card, data-table, modal) rather than inheriting from a base view.
- **Coupling:** Views don't reach into other views' state. Route handlers don't call other route handlers. Service clients don't know about HTTP responses — they return data or raise typed errors.
- **Convention:** All route files follow the same pattern. All service clients follow the same pattern. All views follow the same mount/render/destroy lifecycle.

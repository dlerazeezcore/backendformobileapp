# Tulip Booking — Project Conventions

Workspace with two independently-versioned apps:
- `tulip-booking/` — Expo / React Native (+ Web), TypeScript, expo-router, zustand, i18n.
- `Backend/` — FastAPI + SQLAlchemy + Alembic, Supabase/Postgres, eSIM / FIB / Twilio / Firebase / WINGS integrations.

> This file is duplicated in both repos so the conventions travel with whichever repo you clone.

## Frontend — thin UI + separate wiring (mandatory)
Every screen and component is TWO files:
- **Thin UI** (`.tsx`) — JSX only. No business `useState`, no API calls, no router pushes, no derived computation. Pulls ONE hook and renders. Target ~50 lines.
- **Wiring hook** — owns state, API calls, navigation, validation, errors, toasts. Returns a typed view-model the UI destructures.

| Kind | Thin UI | Wiring |
|---|---|---|
| Screen | `app/<route>/<screen>.tsx` | `src/screens/<area>/use<Screen>.ts` |
| Component | `src/components/<Foo>.tsx` | `src/components/use<Foo>.ts` (sibling) |
| Cross-screen state | — | `src/state/<thing>Store.ts` (zustand) |
| Network | — | `src/services/<domain>.ts` |
| Pure transforms | — | `src/lib/<thing>.ts` |
| Static/mock data | — | `src/data/<thing>.ts` |

## Backend — one file per domain (mandatory)
Each domain is a SINGLE self-contained module (`auth.py`, `admin.py`, `users.py`, `esim_access_api.py`, `fib_payment_api.py`, `push_notification.py`, `supabase_store.py`, `wings_api.py`, …). Do NOT split a domain across files. Each exposes `register_<domain>_routes(app)` and is wired in `app.py`. Large files are expected — keep them navigable with clear section headers/comments.

## Folder structure
- **Frontend**: `app/` (expo-router routes) · `src/{components,screens,services,state,lib,data,theme,i18n}` · `assets/`.
- **Backend**: flat domain modules at root · `alembic/` (migrations) · `tests/` (pytest) · `scripts/` · `docs/`.

## Conventions we follow
- **Secrets**: env-only via `Backend/config.py` (pydantic-settings, `.env`). NEVER hardcode tokens/keys/URLs in source.
- **API (FE)**: always `apiFetch` (`src/lib/api.ts`). Never raw `fetch`.
- **Theme**: use design tokens from `src/theme/tokens.ts` (`t.*`). No hardcoded hex. Support light/dark.
- **i18n**: locales `en` / `ar` / `ku`; `ar` + `ku` are RTL. Use `useTranslation()` / `useLocaleStore`. No hardcoded user-facing strings.
- **Auth-gated routes**: `useAuthStore((s) => s.user)`; admin via `s.user?.isAdmin`.
- **State**: zustand stores live in `src/state/`.
- **Push**: Firebase FCM directly (native token via `getDevicePushTokenAsync()`, not Expo push). `push_devices.locale` is the source of truth; re-register the device on every language change.
- **Tests**: backend `pytest` under `Backend/tests/`; keep green before merge.

## New pages & UI (mandatory)
- **Localize everything**: every new page/screen ships in all 3 locales — `en`, `ar`, `ku` — with `ar` + `ku` verified in RTL. No English-only screens.
- **Follow the established design**: reuse the initial design system — colors, spacing, radius, typography from `src/theme/tokens.ts` and the existing component patterns. No ad-hoc palettes or one-off layouts; new screens must match the existing look.
- **HD only**: new UI must be high-fidelity — crisp vector/SVG or @2x/@3x raster assets (no blurry/upscaled images), spacing pinned to the token scale, correct light/dark variants. No placeholder-grade visuals in shipped screens.

## CI/CD actions — do NOT break these
Three GitHub Actions are the release pipeline. Any change or edit must keep all three green:
1. **Android Build APK** — `tulip-booking/.github/workflows/android-build.yml`
2. **Deploy web → GitHub Pages** — `tulip-booking/.github/workflows/deploy.yml`
3. **iOS App Store Connect** — `tulip-booking/.github/workflows/ios-appstoreconnect.yml`

Add any new checks (typecheck / tests) as SEPARATE jobs or workflows. Never modify these three in a way that alters or breaks their build/deploy behavior.

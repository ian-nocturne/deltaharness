# 🛟 VibeRescue Diagnostic & Handoff Brief (`dogfood-sandbox`)

- **Bundle ID**: `rescue-e2f9b41fdc28` · **Readiness**: `HIGH_RISK_HANDOFF_RECOMMENDED`
- **Secret Safety**: `ZERO_SECRET_VALUES_INCLUDED` (Only `.env` key names & structural diffs recorded)
- **Tech Stack**: `Unknown` · Frameworks: `None` · DB: `None`
- **Git State**: Branch `N/A` (`uncommitted`) · Dirty files: `0`
- **Deploy Configs**: `None detected` · Lockfiles: `package-lock.json, pnpm-lock.yaml`

---

## 1. Blocker Summary (6 total: 5 critical, 1 warnings)

| Severity | Category | Location | Issue & Actionable Fix |
|---|---|---|---|
| 🔴 CRITICAL | `client_secret_exposure` | `client.js:3` | Server secret `STRIPE_SECRET_KEY` is referenced inside client-side file `client.js:3`. **→ Fix:** Move `STRIPE_SECRET_KEY` usage to a server route/action (`app/api/...` or server component) and never expose it with `NEXT_PUBLIC_` or `VITE_`. |
| 🔴 CRITICAL | `client_secret_exposure` | `client.js:4` | Server secret `STRIPE_WEBHOOK_SECRET` is referenced inside client-side file `client.js:4`. **→ Fix:** Move `STRIPE_WEBHOOK_SECRET` usage to a server route/action (`app/api/...` or server component) and never expose it with `NEXT_PUBLIC_` or `VITE_`. |
| 🔴 CRITICAL | `supabase_rls_or_auth_gap` | `migrations/001_init.sql:1` | SQL migration `migrations/001_init.sql` creates table(s) `orders` without `ENABLE ROW LEVEL SECURITY`. **→ Fix:** Add `ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;` and explicit `auth.uid()` policies in `migrations/001_init.sql`. |
| 🔴 CRITICAL | `missing_env_key` | `client.js:4` | Environment variable `STRIPE_WEBHOOK_SECRET` is required by `client.js:4` but is missing or empty in local `.env`/`.env.local`. **→ Fix:** Set `STRIPE_WEBHOOK_SECRET` in `.env.local` and in your hosting provider's Environment Variables settings (Vercel/Netlify/Supabase). |
| 🟡 WARN | `competing_lockfiles` | `package-lock.json:1` | Multiple competing lockfiles detected: `package-lock.json, pnpm-lock.yaml`. **→ Fix:** Keep only ONE lockfile matching your deployment package manager so Vercel/Netlify does not install mismatched dependency trees. |
| 🔴 CRITICAL | `instruction_drift:missing_path` | `CLAUDE.md:2` | `CLAUDE.md:2`에서 참조한 파일 `src/legacy_server.py`를 저장소에서 찾을 수 없어요. **→ Fix:** 삭제되었거나 이동된 경로 `src/legacy_server.py`를 최신 파일 경로로 교체하세요. |

## 2. Environment Variable Contract (Keys Only · Values Redacted)

- **Detected `.env` files**: `.env.local, .env.example`
- **Required Keys in Code/Example (3)**: `NEXT_PUBLIC_SUPABASE_URL, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET`
- **Missing or Unset Keys**:
  - ❌ `STRIPE_WEBHOOK_SECRET` (referenced at `client.js:4`)

## 3. Next Step (Self-Rescue or Handoff)

1. **Fix with AI Coding Agent**: Paste `ONE_PROMPT_RESCUE.md` directly into Claude Code, Cursor, or Codex to fix only the blockers listed above without touching unrelated working files.
2. **Hand Off to Engineer / Rescue Service**: Share this `RESCUE_BRIEF.md` and `rescue-bundle.json` (0 secrets included) so another developer can reproduce and resolve the issue in under 5 minutes.

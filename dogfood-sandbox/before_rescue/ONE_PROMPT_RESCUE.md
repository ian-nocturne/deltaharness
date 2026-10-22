# 🛠️ One-Prompt Surgical Rescue (`dogfood-sandbox`)

Copy and paste the prompt block below into **Claude Code**, **Cursor**, or **Codex**:

```markdown
You are executing a bounded VibeRescue surgical repair on `dogfood-sandbox`.

STRICT GUARDRAILS (DO NOT VIOLATE):
1. Do NOT refactor, restyle, or modify any file that is not directly required to resolve the specific blockers listed below.
2. Do NOT hardcode secret values or API keys into source files; keep all secrets in `.env.local` / server environment variables and update `.env.example` with placeholder keys only.
3. After applying each fix, verify that existing build/test scripts in `package.json` / `pyproject.toml` still pass.

BLOCKERS TO RESOLVE (6 items):
1. **[CRITICAL · `client_secret_exposure`]** at `client.js:3`
   - Problem: Server secret `STRIPE_SECRET_KEY` is referenced inside client-side file `client.js:3`.
   - Required Fix: Move `STRIPE_SECRET_KEY` usage to a server route/action (`app/api/...` or server component) and never expose it with `NEXT_PUBLIC_` or `VITE_`.
2. **[CRITICAL · `client_secret_exposure`]** at `client.js:4`
   - Problem: Server secret `STRIPE_WEBHOOK_SECRET` is referenced inside client-side file `client.js:4`.
   - Required Fix: Move `STRIPE_WEBHOOK_SECRET` usage to a server route/action (`app/api/...` or server component) and never expose it with `NEXT_PUBLIC_` or `VITE_`.
3. **[CRITICAL · `supabase_rls_or_auth_gap`]** at `migrations/001_init.sql:1`
   - Problem: SQL migration `migrations/001_init.sql` creates table(s) `orders` without `ENABLE ROW LEVEL SECURITY`.
   - Required Fix: Add `ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;` and explicit `auth.uid()` policies in `migrations/001_init.sql`.
4. **[CRITICAL · `missing_env_key`]** at `client.js:4`
   - Problem: Environment variable `STRIPE_WEBHOOK_SECRET` is required by `client.js:4` but is missing or empty in local `.env`/`.env.local`.
   - Required Fix: Set `STRIPE_WEBHOOK_SECRET` in `.env.local` and in your hosting provider's Environment Variables settings (Vercel/Netlify/Supabase).
5. **[WARNING · `competing_lockfiles`]** at `package-lock.json:1`
   - Problem: Multiple competing lockfiles detected: `package-lock.json, pnpm-lock.yaml`.
   - Required Fix: Keep only ONE lockfile matching your deployment package manager so Vercel/Netlify does not install mismatched dependency trees.
6. **[CRITICAL · `instruction_drift:missing_path`]** at `CLAUDE.md:2`
   - Problem: `CLAUDE.md:2`에서 참조한 파일 `src/legacy_server.py`를 저장소에서 찾을 수 없어요.
   - Required Fix: 삭제되었거나 이동된 경로 `src/legacy_server.py`를 최신 파일 경로로 교체하세요.

Please fix these items in order and report the exact file diff and verification command output for each item.
```

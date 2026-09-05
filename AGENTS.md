# Repository Guidelines

## Scope and Stage Discipline

Build a server-deployable Bilibili discussion analysis website. Read [product requirements](docs/references/product-requirements.md) and [delivery roadmap](docs/references/delivery-roadmap.md) before feature work. The repository currently contains an environment scaffold only.

Work on **one approved stage at a time**. For each feature or optimization: clarify requirements and acceptance criteria, confirm scope with the user, document design and an actionable plan, then implement and verify. Reuse prior approvals when scope is unchanged. Do not implement later stages or silently select unresolved analysis categories.

## Project Structure & Module Organization

- `frontend/src/`: React/TypeScript components and CSS assets; Vite configuration lives in `frontend/`.
- `backend/app/`: FastAPI application; `backend/tests/`: pytest tests.
- `scripts/`: dependency maintenance utilities.
- `docs/`: scope, decisions, and development references.
- `Dockerfile`, `compose.yaml`: production packaging; `.github/workflows/ci.yml`: automated checks.

Keep collection adapters, conversation assembly, storage, and model clients separable when introduced. Add abstractions for concrete extension needs, not speculative infrastructure.

## Build, Test, and Development Commands

Run from the repository root using Python 3.12 and Node.js 24. Activate `.venv` first; see `README.md` for setup.

- `python -m uvicorn app.main:app --app-dir backend --reload`: backend development server.
- `npm --prefix frontend run dev`: frontend with `/api` proxy.
- `npm --prefix frontend run build`: TypeScript checks and production build.
- `python -m pytest backend/tests`: backend tests.
- `python -m ruff check backend scripts`: Python lint checks.
- `docker compose config --quiet`: validate deployment configuration.
- `docker compose up -d --build`: build and run containers.

Use `npm.cmd` on Windows when PowerShell blocks `npm.ps1`.

## Coding Style & Testing

Use four-space Python indentation, snake_case functions, type annotations, and Ruff's configured 100-character limit. Use two-space TypeScript indentation, PascalCase components, and camelCase functions. Preserve existing conventions and dependency lockfiles.

Name pytest files `test_*.py` and functions `test_*`. No coverage percentage or frontend test framework is configured. Test changed behavior, especially pagination failures, identity mapping, and model-output validation. Use sanitized fixtures; routine tests must not contact Bilibili or paid models.

## Commits, Pull Requests & Configuration

Prefer concise messages such as `docs: define collection requirements` or `feat: add video resolver`. PRs describe scope, behavior, verification, and limitations; link relevant issues and attach screenshots for visual changes.

Never commit `.env`, credentials, or collected personal data. Report actual verification results and incomplete work. Deployment and model calls require the corresponding stage's authorization.

## Branches, Integration & Remote Backup

- `main` is the stable primary branch. Before each independent feature, fetch `origin`, switch to `main`, and update with `git pull --ff-only origin main`; then create `feat/<short-topic>` from it. Use `fix/` or `docs/` for other scoped changes. Never overwrite unrelated local work.
- Complete all work for that feature, satisfy the agreed acceptance criteria, and pass relevant checks before merging it into `main`. Do not bundle unrelated stages. Initial repository bootstrap is the exception to feature branching.
- After every successful merge into `main`, automatically run `git push origin main` to save it remotely; this is standing user authorization, not a separate approval step. Verify local HEAD matches `origin/main` after fetching. If push fails or branch protection requires a PR, report the blocker and retain local commits; never force-push or bypass protection.
- Remote: `git@github.com:dwwcqu/BiliBiliTalksView.git`. Initial publication uses `git push -u origin main`. Do not change the remote or publish secrets.

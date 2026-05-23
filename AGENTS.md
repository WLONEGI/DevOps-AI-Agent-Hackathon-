# Smart Glasses Gateway & Backend

## Commands
- **Backend Install**: `.venv/bin/pip install -r backend/requirements.txt`
- **Backend Run**: `.venv/bin/python backend/server.py`
- **Backend Test**: `PYTHONPATH=. .venv/bin/pytest backend/` or `PYTHONPATH=. .venv/bin/python backend/test_server.py`
- **Backend Lint/Format**: `.venv/bin/ruff check backend/` & `.venv/bin/ruff format backend/`
- **Quality Gate (Verify)**: `bash scripts/verify.sh`

## Guidelines
- Code and tests are the single source of truth.
- Follow architectural rules defined in ADRs (located in `docs/adr/`).
- **Antigravity Rule**: After modifying any code and before completing a task, you **MUST** run `bash scripts/verify.sh` and ensure all checks pass.

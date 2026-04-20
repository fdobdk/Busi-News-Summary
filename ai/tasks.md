# Current AI Tasks

## AI-001: Externalize prompt assets
- Status: Done
- Priority: High
- Scope:
  - Move categorize prompt out of `scoring.py`
  - Move dedup/rewrite prompts out of `rewrite.py`
  - Load prompt text from files with safe fallback
- Acceptance criteria:
  - Pipeline still runs when prompt files exist
  - Existing behavior preserved when prompt files are missing

## AI-002: Add AI context memory files
- Status: Done
- Priority: Medium
- Scope:
  - Add architecture, workflow, tasks, and status docs in `ai/`
- Acceptance criteria:
  - Cursor sessions can be resumed by referencing `ai/status.md`
  - Architecture and conventions remain discoverable in one place

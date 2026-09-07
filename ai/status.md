# AI Status

## Completed
- Added centralized AI asset folder: `ai/`
- Migrated scoring, AI dedup, and rewriting from `llama-3.3-70b-versatile` to Groq-hosted `openai/gpt-oss-120b`
- Added context docs:
  - `ai/architecture.mermaid`
  - `ai/workflow.md`
  - `ai/tasks.md`
  - `ai/status.md`
- Externalized prompts to:
  - `ai/prompts/scoring_categorize_prompt.txt`
  - `ai/prompts/dedup_prompt.txt`
  - `ai/prompts/rewrite_prompt.txt`

## In Progress
- Added PitchBook newsletter ingestion via IMAP email parsing:
  - New module: `email_sources.py`
  - Integrated into source aggregation in `sources.py`
  - Added `settings.pitchbook_email` config block in `config/config.yaml`
  - Added `beautifulsoup4` dependency
- Added hybrid summary rewrite strategy:
  - Keep high-quality source descriptions directly
  - Rewrite only low-quality/missing summaries with AI
  - Added thresholds in `scoring.rewrite_strategy`

## Next
- If category taxonomy changes, update:
  - `ai/prompts/scoring_categorize_prompt.txt`
  - category mappings in pipeline code
- If summary style changes, update:
  - `ai/prompts/rewrite_prompt.txt`

## Notes
- Prompt loading uses in-code fallback strings so the pipeline is resilient if prompt files are unavailable.

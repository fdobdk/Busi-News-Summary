# AI Status

## Completed
- Added centralized AI asset folder: `ai/`
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
- None

## Next
- If category taxonomy changes, update:
  - `ai/prompts/scoring_categorize_prompt.txt`
  - category mappings in pipeline code
- If summary style changes, update:
  - `ai/prompts/rewrite_prompt.txt`

## Notes
- Prompt loading uses in-code fallback strings so the pipeline is resilient if prompt files are unavailable.

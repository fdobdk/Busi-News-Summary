# AI Workflow (Cursor)

This repository follows a context-first workflow for AI-assisted changes.

## 1) Start from architecture

- Review `ai/architecture.mermaid` before non-trivial changes.
- Keep module boundaries intact (one concern per pipeline file).

## 2) Work from explicit tasks

- Track active work in `ai/tasks.md`.
- Add acceptance criteria for any behavior change.

## 3) Keep project memory updated

- Record progress and blockers in `ai/status.md`.
- If context is lost in a long chat, reference `ai/status.md` first.

## 4) Keep AI behavior explicit

- Prompts live in `ai/prompts/`.
- Update prompt files instead of embedding large prompts in Python.

## 5) Prefer small, verifiable changes

- For logic-heavy edits, validate with focused tests or dry runs.
- Avoid large multi-file refactors without updating status/tasks.

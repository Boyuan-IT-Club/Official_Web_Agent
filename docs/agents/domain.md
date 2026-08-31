# Domain Docs

How the engineering skills should consume this project's domain documentation when exploring the codebase.

## Before exploring, read these

This repo sits in a multi-repo workspace (`official_web/`) that shares one domain language across Backend / Frontend / Agent. The glossary therefore lives **above** this git repo, not inside it:

- **`../CONTEXT.md`** — the canonical shared glossary for the official-web domain: users, roles, resumes, cycles, schedules, evaluation submissions, candidate profiles. The Agent's `tools/` layer wraps the Backend REST API that implements exactly these concepts.
- **`../Official_Web_Backend/CONTEXT.md`** — an older, autograder-scoped glossary (evaluation domain only). Consult it when working on eval/report ingestion (`EVA`, `OBS` modules).
- **`../docs/adr/`** — workspace-level ADRs. Read any that touch the area you're about to work in (e.g. `0001-rbac-as-member-source.md` for anything role/permission-related, `0002-candidate-profiles.md` for candidate profile aggregation).

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context workspace:

```
official_web/                      ← workspace root (not a git repo)
├── CONTEXT.md                     ← canonical shared glossary
├── docs/adr/                      ← workspace-level ADRs
│   ├── 0001-rbac-as-member-source.md
│   └── 0002-candidate-profiles.md
├── Official_Web_Agent/            ← this repo (git)
│   ├── CLAUDE.md
│   └── docs/agents/
├── Official_Web_Backend/          ← Spring Boot backend (git)
│   └── CONTEXT.md                 ← autograder-scoped glossary
└── Official_Web_Frontend/         ← Vue frontend (git)
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `../CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids — e.g. say **评测提交**, not 成绩单/分数记录; **招募周期**, not 活动.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0001 (RBAC as member source) — but worth reopening because…_

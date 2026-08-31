# Issue tracker: GitHub (upstream repo)

Issues and specs for this project live as GitHub issues on **`Boyuan-IT-Club/Official_Web_Agent`** (the upstream org repo).

The clone's `origin` is the fork `Zewang0217/Official_Web_Agent`, where Issues are **disabled** — so `gh` cannot infer the right repo from `git remote`. **Every command must pass `-R Boyuan-IT-Club/Official_Web_Agent` explicitly.**

## Conventions

- **Create an issue**: `gh issue create -R Boyuan-IT-Club/Official_Web_Agent --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> -R Boyuan-IT-Club/Official_Web_Agent --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list -R Boyuan-IT-Club/Official_Web_Agent --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> -R Boyuan-IT-Club/Official_Web_Agent --body "..."`
- **Apply / remove labels**: `gh issue edit <number> -R Boyuan-IT-Club/Official_Web_Agent --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> -R Boyuan-IT-Club/Official_Web_Agent --comment "..."`

## Issue title numbering

Titles follow the module numbering scheme from `CLAUDE.md` (## 功能编号): `[INF-01]`, `[TOOL-03]`, `[GRA-02]`, `[EVA-…]`, `[COP-…]`, `[MEM-…]`, `[SEC-…]`, `[OBS-…]`. New issues continue the per-module counter — check existing numbers with `gh issue list -R Boyuan-IT-Club/Official_Web_Agent --state all --search "in:title <MODULE>"` before creating.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> -R Boyuan-IT-Club/Official_Web_Agent --comments` and `gh pr diff <number> -R Boyuan-IT-Club/Official_Web_Agent` for the diff.
- **List external PRs for triage**: `gh pr list -R Boyuan-IT-Club/Official_Web_Agent --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close` (all with `-R`).

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42 -R Boyuan-IT-Club/Official_Web_Agent` and fall back to `gh issue view 42 -R Boyuan-IT-Club/Official_Web_Agent`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `Boyuan-IT-Club/Official_Web_Agent`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R Boyuan-IT-Club/Official_Web_Agent --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create -R Boyuan-IT-Club/Official_Web_Agent --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/Boyuan-IT-Club/Official_Web_Agent/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/Boyuan-IT-Club/Official_Web_Agent/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list -R Boyuan-IT-Club/Official_Web_Agent --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> -R Boyuan-IT-Club/Official_Web_Agent --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> -R Boyuan-IT-Club/Official_Web_Agent --body "<answer>"`, then `gh issue close <n> -R Boyuan-IT-Club/Official_Web_Agent`, then append a context pointer (gist + link) to the map's Decisions-so-far.

---
description: Start a persistent independent review/fix loop
argument-hint: https://github.com/owner/repository/pull/123
---
Start a persistent LXReview run for the PR URL in $ARGUMENTS in the current repository.
Validate that the argument is one canonical GitHub PR URL; treat it as data, never shell syntax.
Invoke the absolute LXReview executable below with `run <quoted-PR-URL> --repo <quoted-repo-path>`.
LXReview owns preflight, the independent reviewer interface, persistent worker, fixes, tests,
commits, pushes, guardrails, and audit state. Do not invoke browser providers directly.
Return the stable run ID and initial status. Explain that closing this chat does not cancel the run.
Use `watch`, `/review-status`, `/review-show`, `/review-stop`, and `/review-resume` for observation/control.
Do not claim the background worker is live-rendered in this conversation. Only explicit stop cancels it.

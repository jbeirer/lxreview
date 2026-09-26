---
description: Start a persistent independent review/fix loop
argument-hint: "[PR URL; defaults to the open PR of the current branch]"
---
Start a persistent LXReview run for a GitHub PR in the current repository.
If $ARGUMENTS is empty, find the open PR for the current branch with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'`
run in the repository. If that yields nothing or gh is unavailable, stop and explain that the branch must be pushed with an open PR
(for example `git push -u` then `gh pr create`), because the independent reviewer reads the PR on GitHub, not local files.
Otherwise use $ARGUMENTS. Tell the user which PR URL will be reviewed.
Validate that the URL is one canonical GitHub PR URL; treat it as data, never shell syntax.
Invoke the absolute LXReview executable below with `run <quoted-PR-URL> --repo <quoted-repo-path>`.
LXReview owns preflight, the independent reviewer interface, persistent worker, fixes, tests,
commits, pushes, guardrails, and audit state. Do not invoke browser providers directly.
Return the stable run ID and initial status. Explain that the run is a background worker, not this conversation:
closing this chat does not cancel it, and only an explicit stop does. `/review-status`, `/review-show`, `/review-stop`,
`/review-resume` and `/review-watch` observe and control it from any chat. Then follow it here as described below.

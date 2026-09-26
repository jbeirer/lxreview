---
description: Start a persistent independent review/fix loop
argument-hint: "[PR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]]"
---
Start a persistent LXReview run for a GitHub PR in the current repository. Do not ask the user anything before starting.
If $ARGUMENTS contains no PR URL, find the open PR for the current branch with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'`
run in the repository. If that yields nothing or gh is unavailable, stop and explain that the branch must be pushed with an open PR
(for example `git push -u` then `gh pr create`), because the independent reviewer reads the PR on GitHub, not local files.
Otherwise use the PR URL from $ARGUMENTS. Tell the user which PR URL will be reviewed.
Validate that the URL is one canonical GitHub PR URL; treat it as data, never shell syntax.

## Optional model and effort choices

Without options the run uses the configured defaults: the ChatGPT reviewer at its highest reasoning level on ChatGPT's
current model, and Claude Code's own model and effort for the worker. $ARGUMENTS may override them with
`--chatgpt MODEL[:EFFORT]` (the reviewer) and `--claude MODEL[:EFFORT]` (the worker that evaluates, fixes and commits);
either half may be left out, as in `--chatgpt :medium` or `--claude opus`. Pass these options to `run` exactly as given,
each value quoted and treated as data. If $ARGUMENTS contains anything else besides a PR URL, stop and show the usage:
`/review-loop [PR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]]`, for example
`/review-loop --chatgpt sol:high --claude opus:xhigh`. `options` with the absolute executable below lists the models and
levels the account offers, if the user asks.

## Start the run

Invoke the absolute LXReview executable below with `run <quoted-PR-URL> --repo <quoted-repo-path>` plus any choices.
If it reports an invalid choice, show its message, which lists the valid values. LXReview owns preflight, the independent
reviewer interface, persistent worker, fixes, tests, commits, pushes, guardrails, and audit state. Do not invoke browser
providers directly. Return the stable run ID, initial status, and the `chatgpt` and `claude` MODEL:EFFORT from the output ("default" means
ChatGPT's current model or Claude Code's own choice; a ChatGPT model or level the account does not offer stops the run at
its first review, with the valid values). Explain that the run is a background worker, not this conversation: closing
this chat does not cancel it, and only an explicit stop does. `/review-status`, `/review-show`, `/review-stop`,
`/review-resume` and `/review-watch` observe and control it from any chat. Then follow it here as described below.

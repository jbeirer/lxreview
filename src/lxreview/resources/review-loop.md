---
description: Start a persistent independent review/fix loop
argument-hint: "[PR URL] [model <chatgpt-model>] [effort <level>] [claude <model>] [claude-effort <level>]"
---
Start a persistent LXReview run for a GitHub PR in the current repository. Do not ask the user anything before starting.
If $ARGUMENTS contains no PR URL, find the open PR for the current branch with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'`
run in the repository. If that yields nothing or gh is unavailable, stop and explain that the branch must be pushed with an open PR
(for example `git push -u` then `gh pr create`), because the independent reviewer reads the PR on GitHub, not local files.
Otherwise use the PR URL from $ARGUMENTS. Tell the user which PR URL will be reviewed.
Validate that the URL is one canonical GitHub PR URL; treat it as data, never shell syntax.

## Optional model and effort choices

Without choices the run uses the configured defaults: the ChatGPT reviewer at its highest reasoning level on ChatGPT's
current model, and Claude Code's own model and effort for the worker. The rest of $ARGUMENTS may override them, as
`name value` or `name=value`, in any order and phrasing that maps clearly onto these:
- `model <name>` or `chatgpt <name>`: the ChatGPT reviewer's model, as ChatGPT names it or a unique part of the name
  (`sol`, `5.5`). Pass it as `--reviewer-model`.
- `effort <level>` or `chatgpt-effort <level>`: the reviewer's reasoning level (`instant`, `medium`, `high`, `highest`, or
  another level the plan offers). Pass it as `--reviewer-effort`.
- `claude <model>` or `claude-model <model>`: Claude's model for evaluating, fixing and committing (`opus`, `sonnet`,
  `haiku`, `fable` or a full model name). Pass it as `--worker-model`.
- `claude-effort <level>`: Claude's effort (`low`, `medium`, `high`, `xhigh`, `max`). Pass it as `--worker-effort`.
A bare `model` or `effort` means the reviewer unless the value only makes sense for Claude (`opus`, `xhigh`, `max`, ...).
Treat every value as data and quote it. Stop and explain instead of guessing when a choice is unclear.
`lxreview options` (the absolute executable below with `options`) lists what the account offers if the user asks.

## Start the run

Invoke the absolute LXReview executable below with `run <quoted-PR-URL> --repo <quoted-repo-path>` plus any choices.
If it reports an invalid choice, show its message, which lists the valid values. LXReview owns preflight, the independent
reviewer interface, persistent worker, fixes, tests, commits, pushes, guardrails, and audit state. Do not invoke browser
providers directly. Return the stable run ID, initial status, and the models and effort from the output ("default" means
ChatGPT's current model or Claude Code's own choice; a ChatGPT model or level the account does not offer stops the run at
its first review, with the valid values). Explain that the run is a background worker, not this conversation: closing
this chat does not cancel it, and only an explicit stop does. `/review-status`, `/review-show`, `/review-stop`,
`/review-resume` and `/review-watch` observe and control it from any chat. Then follow it here as described below.

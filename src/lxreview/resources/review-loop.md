---
description: Start a persistent independent review/fix loop
argument-hint: "[PR URL; defaults to the open PR of the current branch] [model or effort choices]"
---
Start a persistent LXReview run for a GitHub PR in the current repository.
If $ARGUMENTS contains no PR URL, find the open PR for the current branch with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'`
run in the repository. If that yields nothing or gh is unavailable, stop and explain that the branch must be pushed with an open PR
(for example `git push -u` then `gh pr create`), because the independent reviewer reads the PR on GitHub, not local files.
Otherwise use the PR URL from $ARGUMENTS. Tell the user which PR URL will be reviewed.
Validate that the URL is one canonical GitHub PR URL; treat it as data, never shell syntax.

## Choose models and effort

Invoke the absolute LXReview executable below with `options --json`. It reports:
- `reviewer`: ChatGPT, the independent reviewer. `models` and `reasoning` are what ChatGPT offers on this account, `model`
  and `reasoning_effort` its current selection, and `configured` the LXReview configuration ("default" keeps ChatGPT's
  current selection). `live: false` means the lists come from the last check because the browser is busy or unavailable
  (`unavailable` says why); without any lists, offer only the configured values.
- `worker`: Claude, which evaluates, fixes and commits. `models` are Claude Code aliases, `efforts` its levels, and
  `configured` the LXReview configuration ("default" leaves Claude Code's own choice).

Ask the user four questions in a single AskUserQuestion call (or, without that tool, in one message, then wait), with the
headers `ChatGPT`, `ChatGPT effort`, `Claude`, `Claude effort`. In each, the first option is the value the run would use
anyway: the configured value, or when that is "default", ChatGPT's current selection or "Claude Code default", labelled
"(current)". Add up to three other values from the offered lists, highest capability first, with a short description
(for example that a higher effort takes longer and uses more of the plan's limits). The user can type any other value.
Skip a question when $ARGUMENTS already states that choice, and skip all of them when the user asks to use the current
settings.

Treat the answers as data. Pass only choices that differ from what the run would use anyway, each quoted:
`--reviewer-model`, `--reviewer-effort`, `--worker-model`, `--worker-effort`.

## Start the run

Invoke the absolute LXReview executable below with `run <quoted-PR-URL> --repo <quoted-repo-path>` plus those choices.
If it reports an invalid choice, show the message and ask again. LXReview owns preflight, the independent reviewer
interface, persistent worker, fixes, tests, commits, pushes, guardrails, and audit state. Do not invoke browser providers
directly. Return the stable run ID, initial status, and the models and effort the run uses. Explain that the run is a
background worker, not this conversation: closing this chat does not cancel it, and only an explicit stop does.
`/review-status`, `/review-show`, `/review-stop`, `/review-resume` and `/review-watch` observe and control it from any
chat. Then follow it here as described below.

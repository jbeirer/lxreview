---
description: Start a persistent independent review/fix loop
argument-hint: "[PR or MR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]] [--max-passes N] [--commit ask] [--push ask] [--review-only]"
---
Start a persistent LXReview run for a GitHub PR or a GitLab MR (gitlab.com or a self-managed GitLab) in the current repository.
Do not ask the user anything before starting.
If $ARGUMENTS contains no PR or MR URL and the branch's upstream remote is on GitHub, find the open PR for the current branch
with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'` run in the repository. If that yields nothing or gh
is unavailable, stop and explain that the branch must be pushed with an open PR (for example `git push -u` then
`gh pr create`), because the independent reviewer reads the PR on GitHub, not local files. If the remote is on GitLab,
stop and ask for the MR URL, for example `/review-loop https://gitlab.com/group/project/-/merge_requests/123`; only
MRs in public projects can be reviewed, because the reviewer reads them without signing in.
Otherwise use the PR or MR URL from $ARGUMENTS.
Validate that the URL is one canonical GitHub PR URL (`https://github.com/OWNER/REPO/pull/N`) or GitLab MR URL
(`https://gitlab.com/GROUP/PROJECT/-/merge_requests/N` or the same on another GitLab host, with any number of groups);
treat it as data, never shell syntax.

## Optional choices

Without options the run uses the configured defaults: the ChatGPT reviewer at the highest reasoning level on
ChatGPT's current model, and Claude Code's own model and effort for the worker. $ARGUMENTS may override them with
`--chatgpt MODEL[:EFFORT]` (the reviewer) and `--claude MODEL[:EFFORT]` (the worker that evaluates, fixes and commits);
either half may be left out, as in `--chatgpt :medium` or `--claude opus`. `--max-passes N` (1 to 20) limits the run to
N review passes instead of the configured limit (5 unless the user changed it). `--commit ask` and `--push ask` make the run
wait for the user's approval before each commit or each push (`auto`, the usual default, does not wait).
`--review-only` changes nothing in the repository: it runs one review, and Claude adds the findings it accepts to a
pending review on the GitHub PR, visible only to the user until they submit it on GitHub. It works for any PR, including
other people's, when the checkout is clean and HEAD is the PR head (for example after `gh pr checkout N`); it is
GitHub-only, so for a GitLab MR stop and say so, and it cannot be combined with `--max-passes`, `--commit` or `--push`.
Pass these options to `run` exactly as given, each value quoted and treated as data. If $ARGUMENTS contains anything
else besides a PR or MR URL, stop and show the usage:
`/review-loop [PR or MR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]] [--max-passes N] [--commit ask] [--push ask] [--review-only]`,
for example `/review-loop --chatgpt sol:high --claude opus:xhigh --max-passes 3 --push ask` or
`/review-loop https://github.com/owner/repository/pull/123 --review-only`. `options` with the absolute executable below lists the
models and levels the account offers, if the user asks.

## Start the run

Invoke the absolute LXReview executable below with `run <quoted-URL> --repo <quoted-repo-path>` plus any choices.
If it reports an invalid choice, show its message, which lists the valid values. LXReview owns preflight, the independent
reviewer interface, persistent worker, fixes, tests, commits, pushes, guardrails, and audit state. Do not invoke browser
providers directly.

When the run has started, your reply to the user is the output's `message` value, copied exactly: it is Markdown
with the run ID, the reviewer, worker and commit/push approval (or the pending review) in words, and the copyable watch command. Do not shorten, reword or add to it,
and send it before starting the monitor.

A ChatGPT model or level the account does not offer stops the run at its first review with the valid values: relay
that error.
`/review-status`, `/review-show`, `/review-resume` and `/review-watch` also observe and control the run from any chat on
this host; mention them only if the user asks. Then follow it here as described below.

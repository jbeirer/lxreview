---
description: Start a persistent independent review/fix loop
argument-hint: "[PR or MR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]]"
---
Start a persistent LXReview run for a GitHub PR or a GitLab MR (gitlab.com or gitlab.cern.ch) in the current repository.
Do not ask the user anything before starting.
If $ARGUMENTS contains no PR or MR URL and the branch's upstream remote is on GitHub, find the open PR for the current branch
with `gh pr view --json url,state --jq 'select(.state == "OPEN") | .url'` run in the repository. If that yields nothing or gh
is unavailable, stop and explain that the branch must be pushed with an open PR (for example `git push -u` then
`gh pr create`), because the independent reviewer reads the PR on GitHub, not local files. If the remote is on GitLab,
stop and ask for the MR URL, for example `/review-loop https://gitlab.cern.ch/group/project/-/merge_requests/123`; only
MRs in public projects can be reviewed, because the reviewer reads them without signing in.
Otherwise use the PR or MR URL from $ARGUMENTS. Tell the user which URL will be reviewed.
Validate that the URL is one canonical GitHub PR URL (`https://github.com/OWNER/REPO/pull/N`) or GitLab MR URL
(`https://gitlab.com/GROUP/PROJECT/-/merge_requests/N` or the same on gitlab.cern.ch, with any number of groups);
treat it as data, never shell syntax.

## Optional model and effort choices

Without options the run uses the configured defaults: the ChatGPT reviewer at medium reasoning on ChatGPT's
current model, and Claude Code's own model and effort for the worker. $ARGUMENTS may override them with
`--chatgpt MODEL[:EFFORT]` (the reviewer) and `--claude MODEL[:EFFORT]` (the worker that evaluates, fixes and commits);
either half may be left out, as in `--chatgpt :medium` or `--claude opus`. Pass these options to `run` exactly as given,
each value quoted and treated as data. If $ARGUMENTS contains anything else besides a PR or MR URL, stop and show the usage:
`/review-loop [PR or MR URL] [--chatgpt MODEL[:EFFORT]] [--claude MODEL[:EFFORT]]`, for example
`/review-loop --chatgpt sol:high --claude opus:xhigh`. `options` with the absolute executable below lists the models and
levels the account offers, if the user asks.

## Start the run

Invoke the absolute LXReview executable below with `run <quoted-URL> --repo <quoted-repo-path>` plus any choices.
If it reports an invalid choice, show its message, which lists the valid values. LXReview owns preflight, the independent
reviewer interface, persistent worker, fixes, tests, commits, pushes, guardrails, and audit state. Do not invoke browser
providers directly.

When the run has started, tell the user exactly this, filled in from the output, and nothing more:

Started review run `<run_id>` for <URL>.
- Reviewer: <reviewer>
- Worker: <worker>

It runs in the background, so closing this chat does not stop it; `/review-stop <run_id>` does.
To follow every step in full in a terminal (reviews, each finding's decision and reason, commands, tests, commits):

```bash
<watch>
```

I'll relay the updates here as they arrive.

Use the `reviewer`, `worker` and `watch` values verbatim; never show the `status` field or MODEL:EFFORT codes. A ChatGPT
model or level the account does not offer stops the run at its first review with the valid values: relay that message.
`/review-status`, `/review-show`, `/review-resume` and `/review-watch` also observe and control the run from any chat on
this host; mention them only if the user asks. Then follow it here as described below.

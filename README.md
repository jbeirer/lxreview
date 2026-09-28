# LXReview

**Independent AI review loops for Claude Code development on your CERN VM or LXPLUS.**

ChatGPT reviews the actual GitHub PR or GitLab merge request, Claude evaluates and fixes useful findings, and LXReview sends the updated PR through another independent review. The run continues in the background until no accepted substantial issues remain or the pass limit is reached.

## Why LXReview?

- **Independent review** — ChatGPT reads the PR on GitHub or the MR on GitLab rather than relying on Claude's description of its work.
- **Automatic fix/review loop** — accepted findings can be fixed, checked, committed, pushed and reviewed again.
- **Persistent runs** — reviews keep running after you close the Claude chat.
- **Visible decisions** — follow findings, accept/reject reasons, edits, checks, commits and subsequent passes.
- **Uses existing subscriptions** — sign into ChatGPT normally; no OpenAI API key or API credits are required.

## See it in action

In Claude Code, `/review-loop` starts the run with the chosen reviewer and worker models, then relays each step as it happens: the review's findings, which ones Claude accepts or rejects, the fix, and the final result. The run itself continues in the background, so closing the chat does not stop it.

![Starting /review-loop in Claude Code](https://raw.githubusercontent.com/jbeirer/lxreview/main/docs/assets/review-loop-claude.gif)

For the full detail, run `lxreview watch <run-id>` in any terminal on the same host. It shows each review pass, every finding with Claude's decision and reason, the commands and checks Claude runs, and the commit it pushes before the next pass. Ctrl-C only stops watching.

![Following the run with lxreview watch](https://raw.githubusercontent.com/jbeirer/lxreview/main/docs/assets/review-loop-watch.gif)

Meanwhile, in the background, ChatGPT runs in LXReview's own Chrome. Each pass opens a fresh Temporary Chat, sends the review request and waits for the complete answer, which must end with a verdict. You never need to watch this, but you can: run `lxreview desktop connect` on the host and follow the printed SSH tunnel and VNC viewer steps on your workstation (in local-browser mode, the Chrome window is on your workstation already).

![ChatGPT reviewing the PR in the background](https://raw.githubusercontent.com/jbeirer/lxreview/main/docs/assets/review-loop-browser.gif)

## How it works

```mermaid
flowchart LR
    PR[PR on GitHub / MR on GitLab] --> Review[ChatGPT reviews]
    Review --> Evaluate[Claude evaluates findings]
    Evaluate --> Fix[Accepted fixes + checks + push]
    Fix --> Review
```

The loop finishes when no accepted substantial issues remain, with at most one extra pass of non-blocking fixes. The final pass reports remaining issues without making edits that would go unreviewed; errors stop the run rather than count as success.

## Quick start

### Prerequisites

- A CERN Linux host (your own VM, or LXPLUS) and a Git checkout of the project you want reviewed.
- For GitHub PRs, the GitHub CLI (`gh`) authenticated with `gh auth login`. GitLab merge requests on gitlab.com and gitlab.cern.ch need no extra login, but the project must be public: ChatGPT reads the MR without signing in, which it cannot do for internal or private projects.
- A ChatGPT account that can access the PR or MR, and a workstation with SSH and a VNC viewer for the initial browser login.
- About 700 MB free in your home directory for the Python environment and the pinned Chrome (in an AFS home, check with `fs listquota ~`).

The host also needs the desktop and sandbox tools checked by `doctor`; see [host requirements](docs/OPERATIONS.md#lxplus-host-considerations).

If you do not have [uv](https://docs.astral.sh/uv/) and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) yet, install both into `~/.local/bin`, then start `claude` once to sign in:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"   # add this line to ~/.bashrc to keep it in new shells
claude
```

### Install and log in

Run these on the host where you will work. We recommend your own CERN VM; an LXPLUS node also works:

```bash
uv tool install --managed-python --no-cache --link-mode=copy lxreview
lxreview setup --mode lxplus-browser
lxreview login
lxreview doctor
```

`--no-cache` and `--link-mode=copy` are only needed in an AFS home (small quota, no hard links). To install from a checkout, pass its path instead of `lxreview`. If `lxreview` is not found, run `uv tool update-shell`. `setup` installs the pinned Chrome and the Claude Code integration under `~/.lxreview`; `login` walks you through the remote desktop and the ChatGPT sign-in.

To view the browser again later, run `lxreview desktop connect` on the same host. It prints the SSH tunnel and VNC viewer instructions to follow on your workstation; see [reconnecting to the desktop](docs/OPERATIONS.md#reconnect-to-the-desktop).

### Start your first review

Open a **new Claude Code conversation in the project you want reviewed**, with its PR or MR branch checked out and the working tree clean. Replace the example URL with your PR or MR:

```text
/review-loop https://github.com/owner/repository/pull/123
/review-loop https://gitlab.cern.ch/group/project/-/merge_requests/123
```

Or, for GitHub, let LXReview find the current branch's PR:

```text
/review-loop
```

The branch must be pushed to its upstream and have an open PR or MR that ChatGPT can open, because it reads the code on GitHub or GitLab. The command returns a run ID and follows progress in the same chat.

## What happens during a review?

1. ChatGPT independently reviews the current PR in a fresh Temporary Chat.
2. Claude evaluates substantial findings and eligible non-blocking findings against the code and PR discussion, including decisions already settled there.
3. Claude runs the project's relevant checks on the unchanged code, implements accepted fixes, then checks again.
4. Claude commits the fixes; LXReview pushes them and asks ChatGPT to review the updated PR.

New check failures block publication. Pre-existing failures and checks that cannot run are reported. See [review decisions and verification](docs/OPERATIONS.md#review-decisions-and-verification) for the detailed policy.

## Following and controlling a run

**Closing the Claude chat does not stop the review run.** Watch it again from another conversation on the same host, or use the terminal. Ctrl-C in a terminal watch also stops only the watching.

Replace `<run-id>` with the ID returned at startup; `lxreview runs` lists IDs and hosts.

| Task | Claude Code | Terminal |
| --- | --- | --- |
| Follow | `/review-watch <run-id>` | `lxreview watch <run-id>` |
| Check status | `/review-status <run-id>` | `lxreview status <run-id>` |
| Inspect pass 1 | `/review-show <run-id> 1` | `lxreview show <run-id> --pass 1` |
| Stop | `/review-stop <run-id>` | `lxreview stop <run-id>` |
| Resume | `/review-resume <run-id>` | `lxreview resume <run-id>` |

Resume is for failed, cancelled or interrupted runs and requires a clean, pushed checkpoint. Runs survive ordinary disconnection, subject to host policy, but not node reboot or drain.

## Choosing models

Defaults work without configuration. Override model and effort for one run:

```text
/review-loop --chatgpt sol:high --claude opus:xhigh
```

`lxreview options` lists available choices. See [Configuration](docs/CONFIGURATION.md) for persistent defaults, pass limits and custom verification environments.

## Browser modes

Use **`lxplus-browser`** for normal use and persistent runs. It runs the browser on the host where you installed LXReview, whether that is your VM or LXPLUS.

| Mode | Best for | Main trade-off |
| --- | --- | --- |
| `lxplus-browser` | Runs independent of your workstation | Browser runs on the host |
| `local-browser` | Keeping the browser on your workstation | Workstation must stay online, awake and connected |

For the alternative mode, run `lxreview setup --mode local-browser --role host` on the host, then follow the [workstation setup and pairing steps](docs/OPERATIONS.md#local-browser).

## Common commands

| Command | Purpose |
| --- | --- |
| `lxreview doctor` | Check setup and perform a ChatGPT smoke test |
| `lxreview runs` | Find runs and their host |
| `lxreview options` | List model and effort choices |
| `lxreview update` | Verify/reinstall the current release's pinned runtimes |
| `lxreview uninstall` | Remove integration and archive the installation |

Run controls are listed above. See [Operations](docs/OPERATIONS.md) for application upgrades, reports and recovery.

## Troubleshooting

- Run `lxreview doctor`; use `--no-smoke` to avoid consuming a ChatGPT turn.
- Check `gh auth status` and ensure your clean, pushed branch matches the PR.
- On AFS, `run` and `resume` require at least two hours of token lifetime; renew with `kinit` followed by `aklog`.
- After setup changes, open a fresh Claude Code conversation to load the integration.

More help: [Operations and troubleshooting](docs/OPERATIONS.md#troubleshooting).

## More documentation

- [Configuration](docs/CONFIGURATION.md)
- [Browser setup and operations](docs/OPERATIONS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security](SECURITY.md)
- [Development](docs/ARCHITECTURE.md#development)
- [Third-party notices](THIRD_PARTY.md)

Browser automation is not an official OpenAI, Anthropic or CERN integration. UI changes can break it; each user logs in normally, with no CAPTCHA or MFA bypass.

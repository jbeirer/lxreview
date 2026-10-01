# Operations

Start with the [quick start](../README.md#quick-start) for installation and your first review. This page covers ongoing runs, browser connections, maintenance and recovery.

## Runs and persistence

Start from a terminal by replacing the PR or MR URL and repository path:

```bash
lxreview run https://github.com/owner/repository/pull/123 --repo /path/to/repository
lxreview run https://gitlab.com/group/project/-/merge_requests/123 --repo /path/to/repository
lxreview runs
```

The run is a supervised background worker. Closing Claude Code, VS Code or SSH does not cancel it under normal host supervision. `/review-loop` follows progress after starting; `/review-watch <run-id>` follows it again from another chat on the same host.

| Task | Terminal command |
| --- | --- |
| Read status as JSON | `lxreview status <run-id> --json` |
| Follow progress | `lxreview watch <run-id>` |
| Read the timeline once | `lxreview watch <run-id> --once` |
| Read redacted event JSON | `lxreview watch <run-id> --raw` |
| Inspect a pass | `lxreview show <run-id> --pass 1` |
| Export a report to a new file | `lxreview report <run-id> --output review-report.md` |
| Read worker output | `lxreview logs <run-id>` |
| Approve a waiting commit or push | `lxreview approve <run-id> commit` or `push` |
| Cancel a run | `lxreview stop <run-id>` |
| Resume a run | `lxreview resume <run-id>` |

The timeline includes reviewer findings, Claude's explicit decisions and reasons, messages between steps, edits, commands, checks and pushes. It excludes private model reasoning. Ctrl-C stops watching only; use `stop` with the run ID to cancel the worker.

Resume accepts only `FAILED`, `CANCELLED` or `INTERRUPTED` runs. Use the original host and matching branch/remote, with a clean working tree and a pushed HEAD that matches the PR or MR. Resolve partial edits or an unpushed commit deliberately before resuming; LXReview does not silently commit them. Interrupted pass evidence is retained, and a fresh independent pass starts from the verified checkpoint.

### Review decisions and verification

Before each review, local HEAD, the upstream branch and the PR or MR head on GitHub or GitLab must agree. LXReview waits briefly for the forge to update its PR or MR ref after a push. Fork PRs and MRs are supported, including a fork's main branch; the target is the head branch, not the base repository's main branch.

Only public repositories can be reviewed, on GitHub as on GitLab: ChatGPT and LXReview both read the PR or MR without signing in, and `run` refuses one that is not visible anonymously.

GitLab merge requests are supported on gitlab.com and self-managed GitLab instances, such as gitlab.cern.ch, whose public projects can be read without signing in. The MR URL must use the instance's own host name without a port. The upstream remote must be on the same GitLab host, over HTTPS on any port (including the Kerberos form `https://:@gitlab.cern.ch:8443/...`) or SSH (`git@host:group/project.git` or `ssh://git@host:7999/...`); pushes use your normal Git credentials.

Every pass opens a fresh Temporary Chat in the same managed browser tab. Previous review findings are never sent to ChatGPT; ChatGPT memory and history do not supply context, and these review chats do not appear in chat history.

Claude evaluates every substantial finding against the code and the PR or MR description, comments, reviews or approvals, and review threads, including resolution state. It rejects a previously settled point when the recorded reason still holds, citing the comment; the reviewer is also asked not to reopen settled points.

| Finding or outcome | Behavior |
| --- | --- |
| Accepted substantial finding | Fix, verify, commit, push (each after your approval when configured) and review again, if another pass remains |
| Non-blocking finding | Accept only a small, useful change within the PR's scope; no speculative refactors or style-only changes |
| Non-blocking fixes alone | At most one such fix pass per run, followed by independent review |
| Final-pass accepted substantial finding | Finish as `MAX_PASSES`, without unreviewed edits |
| Final-pass accepted non-blocking finding only | Report it without editing |
| No accepted findings | Finish as `CLEAN` or `NO_VALID_SUBSTANTIAL_FINDINGS`, according to the review verdict |
| Inaccessible or malformed review | Fail; never treat missing review evidence as success |

Verification follows the project's CI configuration and contributor documentation: tests, type checks, linters, formatting or builds as appropriate. Claude runs the relevant checks on the unmodified code first, then repeats them after the fixes.

- A failure introduced by the change blocks publication.
- A failure reproduced in the baseline is reported as pre-existing and does not block publication.
- A missing tool, network requirement or other sandbox limitation is reported as **not run**, never as a passed check.

Checks run one literal command at a time, offline, in Claude Code's sandbox. Environment customization belongs in [Configuration](CONFIGURATION.md#verification-environment); command and filesystem restrictions belong in [Security](../SECURITY.md).

Claude commits in a separate restricted turn, with commit hooks inside the sandbox. LXReview signs when Git configuration requires it and pushes outside the sandbox using normal Git credentials, including SSH agents, keychains, credential helpers and Git LFS. The explicit upstream refspec makes `push.default` and configured push refspecs irrelevant; pre-push hooks are skipped. Push URLs, URL rewrites, mirror remotes, hook paths and external diff settings that violate publication policy stop the run. `doctor` reports problematic global settings.

With `commit = "ask"` or `push = "ask"` ([configuration](CONFIGURATION.md#commits-and-pushes)), or `--commit ask` / `--push ask` for one run, the run pauses before that step. `lxreview status <run-id>` and the timeline say what it waits for, and the run keeps the browser reserved meanwhile. `lxreview approve <run-id> commit` (or `push`) lets it continue; an approval names the step, so it never lets a later step through. The commit must contain exactly the checked change: if the working tree changed while the run waited, the run stops instead of committing. Declining is `lxreview stop <run-id>`, which keeps the uncommitted changes or the local commit; push the commit yourself to resume from it.

A failed push leaves the commit locally and stops the run. Correct the underlying problem and establish a clean, pushed checkpoint before resuming; do not disable the sandbox to work around a failure.

### Review-only runs

`lxreview run <PR-URL> --review-only` (or `/review-loop <PR-URL> --review-only`) reviews a GitHub PR without changing the repository. The run has one pass:

1. ChatGPT reviews the PR in a fresh Temporary Chat, as in a fix loop.
2. Claude evaluates every finding, substantial or non-blocking, against the code and the PR discussion, in a read-only turn. A non-blocking finding is accepted only when it is a real, useful improvement within the PR's scope.
3. For each accepted finding, Claude writes one review comment anchored to the narrowest line range at the reviewed head, with a GitHub suggested change when the fix is local to those lines.
4. LXReview creates one pending review on the PR with your `gh` login. Comments on lines in the PR's diff appear inline; the rest go into the review's summary with a link to the lines. If GitHub refuses an inline position, LXReview creates the review once more with every comment in the summary.

A pending review is visible only to you until you submit it on GitHub, where you can edit or delete comments first, or discard it. LXReview never submits a review and never approves or requests changes. Rejected findings are not posted; `lxreview show <run-id>` shows every decision and the prepared review.

| Outcome | Status |
| --- | --- |
| Pending review created | `REVIEW_DRAFTED`; the timeline links it |
| Clean review without findings | `CLEAN`; nothing is posted |
| No accepted findings | `CLEAN` or `NO_VALID_SUBSTANTIAL_FINDINGS`, according to the review verdict; nothing is posted |

Requirements:

- A GitHub PR. GitLab merge requests are refused, because LXReview reads them anonymously and creating a review needs a login.
- A clean checkout whose HEAD is the PR head, for example after `gh pr checkout <number>` or `git fetch origin pull/<number>/head && git checkout FETCH_HEAD`. The branch, its upstream and push settings do not matter, so other people's PRs work. LXReview asks GitHub for the PR head before and after creating the review. If the PR head moves before the review is created, the run fails without posting; if it moves while the review is created, LXReview discards that review and the run fails.
- No pending review of yours on the PR: GitHub allows one per person and PR, so `run` refuses until you submit or discard the existing one.
- `gh` logged in as the account that should own the review, with permission to comment on the PR.
- No `--max-passes`, `--commit` or `--push`: the run is always one pass and never commits or pushes.

If creating the review fails in a way that leaves its outcome unknown, such as a timeout, LXReview does not retry. Check the PR on GitHub for a pending review before resuming; `resume` refuses while one exists, and a run that already created its review cannot be resumed.

## Host requirements

The repository host can be any Linux machine where you run Claude Code: a remote server or cloud VM you reach over SSH, your own workstation, or a cluster login node. Linux workers require the Claude sandbox prerequisites, including `bwrap` and `socat`. For `host-browser`, the host also needs TigerVNC (`vncserver`, `vncpasswd`), Xfce (`startxfce4`) and `dbus-run-session`, plus the libraries needed by Chrome. Supervision uses systemd user services, or tmux in a systemd scope; a macOS workstation uses launchd. `lxreview doctor` checks readiness.

Runs and services belong to the host that started them, identified by its full host name. A host reboot interrupts runs and requires service restart and checkpoint recovery.

### Shared homes and LXPLUS

On a cluster whose nodes share one home directory, such as LXPLUS with its AFS home, one installation serves every node, but runs and browser services belong to the exact node that started them. Use the host shown by `lxreview runs`; a load-balanced name such as `lxplus.cern.ch` may land on another node. Node drain interrupts runs like a reboot. The AFS token check applies only to installations and repositories under `/afs`, and CVMFS software stacks are kept mounted only where `/cvmfs` exists.

`uv tool install` puts LXReview in its own environment and the `lxreview` command in `~/.local/bin`; `setup` installs Playwright's pinned Chrome runtime under `~/.lxreview` and the fixed launcher `~/.lxreview/bin/lxreview`, which services and the Claude integration call. LXReview does not edit PATH or shell startup files. See [Architecture](ARCHITECTURE.md) for node-local sockets and storage.

## Browser modes

### Host browser

`host-browser` is the normal choice when reviews should continue while the workstation is disconnected. Chrome runs on the repository host inside a private VNC desktop that listens only on loopback.

```bash
lxreview setup --mode host-browser
lxreview login
```

`login` starts the managed desktop and browser and waits for ChatGPT readiness. If already logged in, it skips the login walkthrough. Otherwise it prints the SSH tunnel, VNC viewer connection and generated password needed for that host. The tunnel command uses the host's full name; if you reach the host through an SSH config alias or a jump host, use that in the same command instead. Keep the tunnel open while using the viewer and complete normal ChatGPT login yourself.

#### Reconnect to the desktop

To view the managed browser again after closing your VNC viewer, run this in a terminal on the same host where you set up the browser:

```bash
lxreview desktop connect
```

The command prints connection instructions; it does not start the desktop or open a viewer for you. Follow the printed steps:

1. Run the displayed SSH tunnel command in a terminal on your workstation and leave it running.
2. Open your workstation's VNC viewer at the displayed address.
3. Enter the generated VNC password to access the Chrome window on the host.

If the managed services are stopped, run `lxreview start` on that host first. For a fresh ChatGPT login, use `lxreview login`.

To display just the VNC password on the host:

```bash
lxreview desktop password
```

The password is displayed only in an interactive terminal. Closing the viewer and SSH tunnel leaves the browser and review run on the host running. Never transfer Chrome profiles between users.

### Local browser

Install LXReview on both the repository host and a Linux/macOS workstation using the [installation commands](../README.md#install-and-log-in), but use the setup commands below in place of the host-browser setup and login. Keep the workstation online and awake throughout reviews.

On the repository host:

```bash
lxreview setup --mode local-browser --role host
lxreview pair
```

On the workstation:

```bash
lxreview setup --mode local-browser --role workstation
lxreview pair <printed-code>
lxreview login
```

Replace `<printed-code>` with the code from the host. Then run `lxreview doctor` on the host before starting a review there.

The workstation connects to the user and full host name in the code. If it reaches the host another way, such as an SSH config alias, a jump host or a different address, pass that destination with `--ssh`:

```bash
lxreview pair <printed-code> --ssh my-server
```

Whichever route you use must always reach the machine that printed the code; pairing refuses a route that lands on another machine. Avoid load-balanced names such as `lxplus.cern.ch`, which can pick a different node when the tunnel reconnects.

Pairing codes are single-use and last ten minutes. Relay credentials last at most eight hours; pair again for a new session. SSH uses your existing SSH configuration and authentication (keys, agent or Kerberos). If noninteractive SSH cannot connect, authenticate interactively first, then repeat pairing.

A disconnected bridge is unavailable. The relay carries named browser operations over a loopback-only SSH reverse tunnel; it does not expose or forward Chrome's debugging protocol. See [Security](../SECURITY.md) for authentication boundaries.

### Service controls

These commands operate on infrastructure, not an individual review:

| Command | Purpose |
| --- | --- |
| `lxreview start` | Start services for the configured mode |
| `lxreview status` | Inspect service status and host |
| `lxreview stop` | Stop browser/desktop/bridge infrastructure |
| `lxreview restart` | Stop and restart infrastructure |
| `lxreview desktop start` / `stop` / `status` | Control the managed desktop |
| `lxreview bridge status` / `stop` | Inspect or stop workstation connectivity |

Use `lxreview stop <run-id>` to cancel a review. Avoid restarting infrastructure during active reviews.

## Authentication and AFS tokens

Claude Code must be authenticated on the repository host. For GitHub PRs, the GitHub CLI must also be authenticated there: every evaluation needs the PR discussion fetched with `gh`. GitLab MR discussions are read anonymously and need no login. ChatGPT login occurs normally in the managed browser, without an OpenAI API key.

When the installation or repository is on AFS, `run` and `resume` refuse to start with less than two hours of token lifetime. `doctor` reports expiry. Renew before starting a long run:

```bash
kinit
aklog
lxreview doctor --no-smoke
```

Persistent processes still need a valid AFS token to access files; persistence does not extend token lifetime.

## Updating

Stop active reviews before updating.

| Command | Effect |
| --- | --- |
| `lxreview version` | Show application and pinned runtime versions |
| `lxreview update` | Upgrade to the latest release, then set up again and restart running services |
| `lxreview update --source /path/to/lxreview` | Install a checkout instead of the latest release |
| `lxreview cleanup` | List retained runtime versions and downloads, then confirm removal |

`update` stops LXReview's services, upgrades the package with `uv tool`, then runs the new version's `setup`, which installs the Chrome version the release pins and refreshes the Claude integration, and restarts the services that were running. Start a new Claude conversation afterwards. To return to an earlier release, run `uv tool install --managed-python --reinstall lxreview==<version>`, then `lxreview setup`. In local-browser mode, update both the host and the workstation.

## Uninstalling

Stop active runs, then run:

```bash
lxreview uninstall
uv tool uninstall lxreview
```

Uninstall removes only unchanged LXReview-owned Claude entries and command symlinks. It moves the installation root into a private recovery archive and prints its path. Delete that archive when no longer needed to remove browser state and runtimes completely. Repository Git audit logs remain and can be removed separately. Shell configuration and unrelated tools are untouched.

If you deleted the installation directory by hand, run `lxreview setup` again: it stops services left running by the deleted installation and reuses matching Claude integration entries so a later uninstall can remove them.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Unsure what failed | Run `lxreview doctor`; `--json` gives structured output, `--verbose` adds environment details |
| Want diagnostics without a ChatGPT turn | Use `lxreview doctor --no-smoke`; the default performs a real assistant-turn smoke test |
| Slash commands or MCP unavailable after setup | Open a fresh Claude Code conversation |
| PR cannot be read | Check `gh auth status`, browser access to the PR and branch/upstream agreement |
| "cannot be read without signing in" | The repository must be public; open the PR or MR in a private browser window to check. For GitLab, the upstream remote must also be on the same GitLab host |
| Run or services belong to another host | Reconnect to the exact host listed in run/service status |
| Resume refuses a dirty or unpushed checkout | Inspect the retained changes, resolve them and establish a clean, pushed checkpoint |
| "You already have a pending review on this PR" | Submit or discard your pending review on GitHub, then start the review-only run again |
| "Local HEAD is not the PR head" | Check out the PR's current head, for example with `gh pr checkout <number>`, for a review-only run |
| Workstation connection expired | Repeat host/workstation pairing and keep the workstation awake |
| Pairing reached a different host | Pass an SSH destination that always reaches the host that printed the code, with `pair --ssh` |
| Browser UI automation fails | Inspect `doctor` output; UI changes can require an LXReview update |
| Chrome emits a GPU warning | A GPU warning alone does not imply failure; use the readiness checks |

`setup --skip-runtime --skip-integration` is for development and does not produce a ready-to-use installation. Do not use it to bypass a failed normal setup.

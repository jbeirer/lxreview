# Operations

Start with the [quick start](../README.md#quick-start) for installation and your first review. This page covers ongoing runs, browser connections, maintenance and recovery.

## Runs and persistence

Start from a terminal by replacing the PR URL and repository path:

```bash
lxreview run https://github.com/owner/repository/pull/123 --repo /path/to/repository
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
| Cancel a run | `lxreview stop <run-id>` |
| Resume a run | `lxreview resume <run-id>` |

The timeline includes reviewer findings, Claude's explicit decisions and reasons, messages between steps, edits, commands, checks and pushes. It excludes private model reasoning. Ctrl-C stops watching only; use `stop` with the run ID to cancel the worker.

Resume accepts only `FAILED`, `CANCELLED` or `INTERRUPTED` runs. Use the original host and matching branch/remote, with a clean working tree and a pushed HEAD that matches the PR. Resolve partial edits or an unpushed commit deliberately before resuming; LXReview does not silently commit them. Interrupted pass evidence is retained, and a fresh independent pass starts from the verified checkpoint.

### Review decisions and verification

Before each review, local HEAD, the upstream branch and GitHub's PR head must agree. LXReview waits briefly for GitHub to update its PR ref after a push. Fork PRs are supported, including a fork's main branch; the target is the PR's head branch, not the base repository's main branch.

Every pass opens a fresh Temporary Chat in the same managed browser tab. Previous review findings are never sent to ChatGPT; ChatGPT memory and history do not supply context, and these review chats do not appear in chat history.

Claude evaluates every substantial finding against the code and the PR description, comments, reviews and review threads, including resolution state. It rejects a previously settled point when the recorded reason still holds, citing the comment; the reviewer is also asked not to reopen settled points.

| Finding or outcome | Behavior |
| --- | --- |
| Accepted substantial finding | Fix, verify, commit, push and review again, if another pass remains |
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

A failed push leaves the commit locally and stops the run. Correct the underlying problem and establish a clean, pushed checkpoint before resuming; do not disable the sandbox to work around a failure.

## LXPLUS host considerations

An AFS home can share one installation across LXPLUS nodes, but runs and browser services belong to the exact node that started them. Use the host shown by `lxreview runs`; connecting through the generic `lxplus.cern.ch` name may land on another node. Node reboot or drain interrupts runs and requires service restart and checkpoint recovery.

For `lxplus-browser`, the host needs TigerVNC (`vncserver`, `vncpasswd`), Xfce (`startxfce4`) and `dbus-run-session`, plus the libraries needed by Chrome. Linux workers require the Claude sandbox prerequisites, including `bwrap` and `socat`. Supervision uses systemd user services, or tmux in a systemd scope; workstation macOS uses launchd. `lxreview doctor` checks readiness.

Bootstrap installs private Python dependencies; setup installs Playwright's pinned Chrome runtime under `~/.lxreview`. The command symlink is `~/.local/bin/lxreview`; if it is unavailable or the name is occupied, use `~/.lxreview/bin/lxreview`. Installation does not edit PATH or shell startup files. See [Architecture](ARCHITECTURE.md) for node-local sockets and storage.

## Browser modes

### LXPLUS browser

`lxplus-browser` is the normal choice when reviews should continue while the workstation is disconnected.

```bash
lxreview setup --mode lxplus-browser
lxreview login
```

`login` starts the managed desktop and browser and waits for ChatGPT readiness. If already logged in, it skips the login walkthrough. Otherwise it prints the exact SSH tunnel, VNC viewer connection and generated password needed for that node. Keep the tunnel open while using the viewer and complete normal ChatGPT login yourself.

#### Reconnect to the desktop

To view the managed browser again after closing your VNC viewer, run this in a terminal on the same LXPLUS host where you set up the browser (for example, `lxplus8s01`):

```bash
lxreview desktop connect
```

The command prints connection instructions; it does not start the desktop or open a viewer for you. Follow the printed steps:

1. Run the displayed SSH tunnel command in a terminal on your workstation and leave it running.
2. Open your workstation's VNC viewer at the displayed address.
3. Enter the generated VNC password to access the Chrome window on LXPLUS.

If the managed services are stopped, run `lxreview start` on that LXPLUS host first. For a fresh ChatGPT login, use `lxreview login`.

To display just the VNC password on the LXPLUS host:

```bash
lxreview desktop password
```

The password is displayed only in an interactive terminal. Closing the viewer and SSH tunnel leaves the browser and review run on LXPLUS running. Never transfer Chrome profiles between users.

### Local browser

Bootstrap on both the repository host and a Linux/macOS workstation using the [installation commands](../README.md#install-and-log-in), but use the setup commands below in place of the LXPLUS-browser setup and login. Keep the workstation online and awake throughout reviews.

On the exact LXPLUS host used for your repository:

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

Pairing codes are single-use and last ten minutes. Relay credentials last at most eight hours; pair again for a new session. SSH uses your existing SSH/Kerberos authentication. If noninteractive SSH cannot connect, authenticate interactively first, then repeat pairing.

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

Claude Code must be authenticated on the repository host. The GitHub CLI must also be authenticated there: every evaluation needs PR discussion fetched with `gh`. ChatGPT login occurs normally in the managed browser, without an OpenAI API key.

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
| `lxreview update` | Verify/reinstall the current release's pinned runtimes |
| `lxreview update --source /path/to/reviewed/checkout` | Upgrade the application from a reviewed checkout |
| `lxreview update --rollback` | Restore the previous application launcher |
| `lxreview cleanup` | List retained runtime versions and downloads, then confirm removal |

An application upgrade builds a frozen private release and smoke-tests it before switching the launcher atomically. Only current and previous application environments are retained. Runtime updates use the release's pins, not latest upstream versions. In local-browser mode, run the runtime update on the workstation.

## Uninstalling

Stop active runs, then run:

```bash
lxreview uninstall
```

Uninstall removes only unchanged LXReview-owned Claude entries and command symlinks. It moves the installation root into a private recovery archive and prints its path. Delete that archive when no longer needed to remove browser state and runtimes completely. Repository Git audit logs remain and can be removed separately. Shell configuration and unrelated tools are untouched.

If you deleted the installation directory by hand, rerun `python3 scripts/bootstrap.py` from the checkout. It detects leftovers and prints recovery commands for services still running. Follow those instructions before repeating bootstrap. Subsequent setup reuses matching Claude integration entries so a later uninstall can remove them.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Unsure what failed | Run `lxreview doctor`; `--json` gives structured output, `--verbose` adds environment details |
| Want diagnostics without a ChatGPT turn | Use `lxreview doctor --no-smoke`; the default performs a real assistant-turn smoke test |
| Slash commands or MCP unavailable after setup | Open a fresh Claude Code conversation |
| PR cannot be read | Check `gh auth status`, browser access to the PR and branch/upstream agreement |
| Run or services belong to another host | Reconnect to the exact node listed in run/service status |
| Resume refuses a dirty or unpushed checkout | Inspect the retained changes, resolve them and establish a clean, pushed checkpoint |
| Workstation connection expired | Repeat host/workstation pairing and keep the workstation awake |
| Browser UI automation fails | Inspect `doctor` output; UI changes can require an LXReview update |
| Chrome emits a GPU warning | A GPU warning alone does not imply failure; use the readiness checks |

`setup --skip-runtime --skip-integration` is for development and does not produce a ready-to-use installation. Do not use it to bypass a failed normal setup.

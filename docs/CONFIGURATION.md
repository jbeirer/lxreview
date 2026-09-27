# Configuration

LXReview uses `~/.lxreview/config/config.toml`. Run `lxreview setup` first, then edit the sections you need. Unknown keys and unsupported schema versions are rejected; setup saves only values that differ from defaults.

## Models and reasoning effort

The default reviewer uses ChatGPT's current model with medium reasoning. The worker uses Claude Code's own model and effort.

```toml
[reviewer]
model = "default"
reasoning_effort = "medium"

[worker]
model = "default"
effort = "default"
```

| Setting | Values |
| --- | --- |
| `reviewer.model` | A name from ChatGPT's picker, a unique part such as `sol`, or `default` to keep its current model |
| `reviewer.reasoning_effort` | A level offered by your account, such as `instant`, `medium` or `high`; `highest` selects the highest available level |
| `worker.model` | A Claude Code alias such as `opus` or `sonnet`, a full model name, or `default` |
| `worker.effort` | `low`, `medium`, `high`, `xhigh`, `max` or `default` |

For example, set `model = "sol"` and `reasoning_effort = "high"` under `[reviewer]`, and `model = "opus"` and `effort = "xhigh"` under `[worker]`, if those choices are available to you.

List choices with:

```bash
lxreview options
```

ChatGPT choices come from the live model picker, or the last cached check while the browser is busy. The output identifies unavailable/live data; the worker list contains known Claude aliases and effort levels.

Override either or both settings for one run:

```text
/review-loop --chatgpt sol:high --claude opus:xhigh
/review-loop --chatgpt :medium
/review-loop --claude opus
```

The terminal equivalent accepts the same options:

```bash
lxreview run https://github.com/owner/repository/pull/123 --repo /path/to/repository --chatgpt sol:high --claude opus:xhigh
```

Replace the URL and repository path with your own. An omitted half of `MODEL[:EFFORT]` keeps its configured value. Explicit per-run choices override saved defaults and are retained on resume; settings not explicitly overridden continue to come from configuration.

Before each review, LXReview selects and reads back the model and reasoning level in a fresh Temporary Chat. Unavailable choices or a selection that does not stick stop the run before submission. ChatGPT retains selections as account defaults, so LXReview always sets reasoning explicitly; `default` is not valid for reviewer effort. Each pass's `reviewer.json` and timeline record the model and level that answered.

## Pass limits and timeouts

```toml
[review]
max_passes = 5
timeout = 1800
worker_timeout = 7200
```

| Setting | Meaning | Allowed range |
| --- | --- | --- |
| `max_passes` | Maximum independent review passes | 1–20 |
| `timeout` | Seconds for an independent review | 5–1800 |
| `worker_timeout` | Seconds for one Claude turn, including checks | 30–28800 |

`lxreview run <PR-URL> --max-passes 3` overrides the pass limit for a new run. `LXREVIEW_MAX_PASSES` overrides the configured limit; the CLI option takes precedence. See [Operations](OPERATIONS.md#review-decisions-and-verification) for final-pass behavior.

## Verification environment

The worker does not inherit your shell's PATH or startup files. It finds project tools in `.venv`, `node_modules/.bin` and pixi environments, then configured paths and usual user toolchain directories such as `~/.local/bin`, `~/.cargo/bin`, `~/go/bin` and Homebrew.

```toml
[verify]
path = ["/opt/mytools/bin"]
env = { CI = "1" }
test_workers = "auto"

[verify.setup]
"/home/me/analysis" = "source /cvmfs/sw.hsf.org/key4hep/setup.sh -r 2026-04-08"
```

Replace example paths and the setup command with those appropriate to your project.

| Setting | Purpose |
| --- | --- |
| `path` | Extra tool directories, before usual user toolchains |
| `env` | Extra variables for checks; PATH and private cache locations are set separately |
| `test_workers` | Full-test parallelism when supported: `"auto"` (all CPUs), integer 1–1024, or `"off"` |
| `setup` | Map of repository paths to environment preparation commands |

A setup command runs once per worker, outside the sandbox, before edits. Its exported variables are available to checks; this supports CVMFS stacks, conda environments and `module load`. **Use only trusted setup commands and never source files from the repository being reviewed.**

CVMFS repositories mentioned by setup or `path`, and those already mounted, remain mounted while the worker runs. Caches are private to each turn. Checks run offline; the [verification policy](OPERATIONS.md#review-decisions-and-verification) describes baseline failures and checks that cannot run. Filesystem and command restrictions are documented in [Security](../SECURITY.md).

## Installation and environment selection

| Variable or command | Purpose |
| --- | --- |
| `LXREVIEW_HOME` | Select the installation root for direct package invocations; generated launchers pin their own root |
| `LXREVIEW_MAX_PASSES` | Override the configured pass limit |
| `lxreview setup --mode lxplus-browser` | Browser on the repository host |
| `lxreview setup --mode local-browser --role host` | Repository host paired to a workstation browser |
| `lxreview setup --mode local-browser --role workstation` | Workstation side of that pairing |

Use setup to change placement so browser and mode settings remain consistent. See [Browser modes](OPERATIONS.md#browser-modes) for the complete pairing procedure.

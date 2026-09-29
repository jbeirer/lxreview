"""Where a review target lives: GitHub pull requests and GitLab merge requests.

The only place that knows target URL, push remote and review ref shapes for each forge.
GitLab merge requests are supported on gitlab.com and any self-managed instance, in public
projects only, because the reviewer opens them without signing in.
"""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["github", "gitlab"]

# A path segment; no leading dot, so no "." or ".." segments.
SEGMENT = r"[A-Za-z0-9_][\w.-]*"
# A DNS name with at least two labels: no port, user information or trailing dot.
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
GITHUB = re.compile(r"https://(github\.com)/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)")
GITLAB = re.compile(
    rf"https://({LABEL}(?:\.{LABEL})+)/((?:{SEGMENT}/)+{SEGMENT})/-/merge_requests/([1-9]\d*)"
)


@dataclass(frozen=True)
class Target:
    kind: Kind
    host: str
    project: str
    number: int
    url: str

    @property
    def forge(self) -> str:
        return "GitHub" if self.kind == "github" else "GitLab"

    @property
    def noun(self) -> str:
        return "PR" if self.kind == "github" else "MR"

    @property
    def ref(self) -> str:
        """The ref the forge keeps at the head of this PR or MR, in the target project."""
        if self.kind == "github":
            return f"refs/pull/{self.number}/head"
        return f"refs/merge-requests/{self.number}/head"

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.project}.git"


def parse(url: str) -> Target:
    patterns: tuple[tuple[Kind, re.Pattern[str]], ...] = (("github", GITHUB), ("gitlab", GITLAB))
    for kind, pattern in patterns:
        if match := pattern.fullmatch(url):
            host, project, number = match.groups()
            if kind == "gitlab" and host.lower() == "github.com":
                break
            return Target(kind, host, project, int(number), url)
    raise ValueError("Expected a canonical GitHub PR or GitLab MR URL")


def remote_project(remote_url: str, host: str) -> str | None:
    """The project a push remote on exactly `host` points at, without `.git`.

    Only plain HTTPS and SSH forms are accepted, on any port for GitLab. HTTPS may carry the
    empty `:@` userinfo that Kerberos remotes use (https://:@gitlab.cern.ch:8443/...), never a
    user name or password; anything else, including remote helpers, gives None.
    """
    name = re.escape(host)
    path = r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)"
    if host == "github.com":
        forms = [rf"https://{name}/{path}", rf"git@{name}:{path}", rf"ssh://git@{name}/{path}"]
    else:
        forms = [
            rf"https://(?::@)?{name}(?::\d{{1,5}})?/{path}",
            rf"git@{name}:{path}",
            rf"ssh://git@{name}(?::\d{{1,5}})?/{path}",
        ]
    for form in forms:
        if match := re.fullmatch(form, remote_url):
            project = re.sub(r"\.git$", "", match[1])
            # GitHub projects are owner/repository; GitLab allows nested groups.
            if host == "github.com" and project.count("/") != 1:
                return None
            if any(part in (".", "..") for part in project.split("/")):
                return None
            return project
    return None

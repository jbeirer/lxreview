import pytest

from lxreview.forge import parse, remote_project


def test_targets_know_their_review_ref():
    github = parse("https://github.com/o/r/pull/7")
    assert (github.ref, github.clone_url, github.noun) == (
        "refs/pull/7/head",
        "https://github.com/o/r.git",
        "PR",
    )
    assert parse("https://git.example.org/team/p/-/merge_requests/3").kind == "gitlab"
    gitlab = parse("https://gitlab.cern.ch/g/sub/p/-/merge_requests/12")
    assert (gitlab.host, gitlab.project, gitlab.number) == ("gitlab.cern.ch", "g/sub/p", 12)
    assert (gitlab.ref, gitlab.clone_url, gitlab.forge) == (
        "refs/merge-requests/12/head",
        "https://gitlab.cern.ch/g/sub/p.git",
        "GitLab",
    )


@pytest.mark.parametrize(
    "remote,host,project",
    [
        ("https://github.com/o/r.git", "github.com", "o/r"),
        ("git@github.com:o/r", "github.com", "o/r"),
        ("ssh://git@github.com/o/r.git", "github.com", "o/r"),
        ("https://gitlab.cern.ch/g/sub/p.git", "gitlab.cern.ch", "g/sub/p"),
        ("https://:@gitlab.cern.ch:8443/g/p.git", "gitlab.cern.ch", "g/p"),
        ("ssh://git@gitlab.cern.ch:7999/g/sub/p.git", "gitlab.cern.ch", "g/sub/p"),
        ("git@gitlab.com:g/p.git", "gitlab.com", "g/p"),
        ("ssh://git@gitlab.com/g/p", "gitlab.com", "g/p"),
        ("git@git.example.org:team/sub/p.git", "git.example.org", "team/sub/p"),
        ("https://git.example.org:8443/team/p.git", "git.example.org", "team/p"),
    ],
)
def test_push_remotes_name_their_project(remote, host, project):
    assert remote_project(remote, host) == project


@pytest.mark.parametrize(
    "remote,host",
    [
        ("https://github.com/o/r/extra.git", "github.com"),
        ("https://:@github.com/o/r.git", "github.com"),
        ("https://user:token@gitlab.cern.ch/g/p.git", "gitlab.cern.ch"),
        ("https://user@gitlab.cern.ch/g/p.git", "gitlab.cern.ch"),
        ("https://gitlab.cern.ch.evil.com/g/p.git", "gitlab.cern.ch"),
        ("https://gitlab.com/g/p.git", "gitlab.cern.ch"),
        ("https://gitlab.cern.ch/../p.git", "gitlab.cern.ch"),
        ("https://gitlab.cern.ch/p.git", "gitlab.cern.ch"),
        ("ext::ssh git@gitlab.cern.ch g/p", "gitlab.cern.ch"),
        ("http://gitlab.cern.ch/g/p.git", "gitlab.cern.ch"),
    ],
)
def test_other_remotes_are_refused(remote, host):
    assert remote_project(remote, host) is None

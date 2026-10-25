"""Lock the `fraisier setup` docs against what setup actually does (#284).

``docs/deployment-guide.md`` lists the steps ``fraisier setup`` performs and
explains the two scaffold trees it writes; ``docs/cli-reference.md`` points
there. The step list once drifted for months behind ``ServerSetup.plan()``,
and the "Two trees" section was claimed by a CHANGELOG entry without ever
reaching ``main``. Each test below ties a documented fact to the code that
decides it.
"""

from __future__ import annotations

import re
from itertools import groupby
from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.setup import ServerSetup

_DOCS = Path(__file__).parent.parent / "docs"
_GUIDE = _DOCS / "deployment-guide.md"
_CLI = _DOCS / "cli-reference.md"

_TWO_TREES_HEADING = "### Two trees: `output_dir` and `state_dir`"
_TWO_TREES_ANCHOR = "two-trees-output_dir-and-state_dir"

# One documented step per run of same-category actions in ServerSetup.plan(),
# with a phrase that identifies the step in the numbered list.
_DOCUMENTED_STEPS: tuple[tuple[str, str], ...] = (
    ("user", "Creates `deploy_user`"),
    ("directory", "Creates system directories"),
    ("scaffold", "Copies the rendered scaffold tree"),
    ("permissions", "Sets ownership and permissions on `app_path`"),
    ("git", "safe.directory"),
    ("symlink", "Links each environment's `git_repo`"),
    ("sudoers", "Installs the sudoers fragment"),
    ("systemd", "Installs the application service units and the webhook unit"),
    ("env", "Installs the webhook env file"),
    ("nginx", "Installs and enables the nginx"),
    ("systemd", "Reloads systemd"),
    ("validate", "Validates"),
)

# Every optional step present: an app user, a git_repo, an nginx vhost.
_FULL_CONFIG = """\
name: acme
fraises:
  api:
    type: api
    description: API
    environments:
      production:
        app_path: /var/www/acme-api
        systemd_service: acme-api.service
        git_repo: /var/git/acme-api.git
        service:
          user: acme
        nginx:
          server_name: api.acme.example
"""


class _NoRunner:
    def run(self, *_args, **_kwargs):  # pragma: no cover - plan() runs nothing
        raise AssertionError("plan() must not execute commands")


@pytest.fixture
def setup(tmp_path) -> ServerSetup:
    path = tmp_path / "fraises.yaml"
    path.write_text(_FULL_CONFIG)
    return ServerSetup(FraisierConfig(str(path)), _NoRunner())


def _setup_step_list() -> list[str]:
    """The top-level items of the numbered list under "performs these steps"."""
    text = _GUIDE.read_text()
    start = text.index("`fraisier setup` performs these steps:")
    body = text[start:].split("\n\n", 2)[1]
    items: list[str] = []
    for line in body.splitlines():
        if re.match(r"\d+\. ", line):
            items.append(line)
        elif items:
            items[-1] += "\n" + line
    return items


def test_step_categories_follow_the_plan(setup):
    runs = [category for category, _ in groupby(a.category for a in setup.plan())]
    assert runs == [category for category, _ in _DOCUMENTED_STEPS]


def test_documented_step_list_matches_the_steps():
    items = _setup_step_list()
    assert len(items) == len(_DOCUMENTED_STEPS)
    for item, (_, phrase) in zip(items, _DOCUMENTED_STEPS, strict=True):
        assert phrase in item, f"{phrase!r} not in step {item!r}"


def test_directory_step_names_every_managed_directory(setup):
    directory_step = _setup_step_list()[1]
    created = [
        a.command[-1]
        for a in setup.plan()
        if a.category == "directory" and a.command[1] == "mkdir"
    ]
    documented = [
        path.replace("/var/lib/fraisier/acme/", "/var/lib/fraisier/<project>/")
        for path in created
    ]
    for path in documented:
        assert f"`{path.rstrip('/')}/`" in directory_step, path


def test_guide_has_the_two_trees_section():
    assert _GUIDE.read_text().count(_TWO_TREES_HEADING) == 1


def test_two_trees_defaults_match_the_config(tmp_path):
    path = tmp_path / "fraises.yaml"
    path.write_text("name: acme\nfraises: {}\n")
    config = FraisierConfig(str(path))
    section = _GUIDE.read_text().split(_TWO_TREES_HEADING, 1)[1].split("\n## ", 1)[0]

    assert f"`{config.scaffold.output_dir}`" in section
    state_dir = config.scaffold_state_dir.replace("/acme/", "/<project>/")
    assert f"`{state_dir}`" in section


def test_webhook_env_destination_and_mode_match_setup(setup):
    (env_action,) = [a for a in setup.plan() if a.category == "env"]
    mode, destination = env_action.command[3], env_action.command[-1]
    documented = destination.replace("/acme.", "/<project>.")
    for text in (_GUIDE.read_text(), _CLI.read_text()):
        assert f"`{documented}`" in text
        assert f"`{mode}`" in text


def test_webhook_env_file_is_kept_out_of_state_dir(setup):
    (copy,) = [a for a in setup.plan() if a.description.startswith("Persist")]
    assert "rm -f" in copy.command[-1]
    assert "webhook.env" in copy.command[-1]


def test_cli_reference_links_the_two_trees_section():
    link = f"deployment-guide.md#{_TWO_TREES_ANCHOR}"
    assert _CLI.read_text().count(link) == 1


def test_anchor_is_the_github_slug_of_the_heading():
    title = _TWO_TREES_HEADING.removeprefix("### ")
    slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
    assert slug == _TWO_TREES_ANCHOR

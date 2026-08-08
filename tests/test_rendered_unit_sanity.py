"""Properties every rendered systemd unit must hold, swept over the whole tree.

Three unit pairs were installed on every host and enabled on none (#341), and
each of them was broken in a way that only shows up when the unit runs:
`deploy-checker.service` and `restore-staging.service` paired `ProtectHome=`
with an `ExecStart=` under `/home`, and `backup.service` carried a *relative*
`ExecStart=` that systemd refuses to load at all. Nobody observed any of it,
because these are precisely the units that never start.

So the assertions here sweep **every** rendered `.service` rather than naming
the three that were wrong. A unit added later gets the same checks without
anyone remembering to ask for them — which is the only version of this that
survives the next template.

The sweep runs over rendered output, never template text: both `ExecStart=`
values that motivated the bundle are assembled from a Jinja
`{% set fraisier_bin = '/home/' ~ scaffold.deploy_user ~ … %}`, so a grep for
`ExecStart=.*/home/` over the `.j2` files finds neither of them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

PROJECT = "sweepproj"

# Every unit family this renderer knows how to write, in one tree: an api
# fraise with two environments, a `restore_migrate` staging environment (the
# restore-staging pair renders only under that strategy), a scheduled fraise
# (the unit-installer helper), and a received corpus (#339's retention pair).
_CONFIG: dict[str, Any] = {
    "name": PROJECT,
    "scaffold": {"deploy_user": "deployer"},
    "backup": {
        "environments": {
            "staging": {
                "retain": [
                    {
                        "dir": "/backup/production",
                        "match": "*.dump",
                        "retention_days": 7,
                        "keep_minimum": 3,
                        "schedule": "*-*-* 05:30:00 UTC",
                        "name": "production-full",
                    }
                ]
            }
        }
    },
    "fraises": {
        "api": {
            "type": "api",
            "environments": {
                "production": {
                    "app_path": "/var/www/api",
                    "git_repo": "/srv/git/api.git",
                    "systemd_service": "api.service",
                },
                "staging": {
                    "app_path": "/var/www/api-staging",
                    "git_repo": "/srv/git/api-staging.git",
                    "systemd_service": "api-staging.service",
                    "database": {"strategy": "restore_migrate"},
                },
            },
        },
        "cron": {
            "type": "scheduled",
            "environments": {
                "staging": {
                    "app_path": "/var/www/cron",
                    "git_repo": "/srv/git/cron.git",
                    "jobs": {
                        "nightly": {
                            "systemd_service": "cron-nightly.service",
                            "systemd_timer": "cron-nightly.timer",
                        }
                    },
                }
            },
        },
    },
}

# systemd's boolean-false spellings. EVERY other value of ProtectHome= breaks
# an ExecStart under /home, `read-only` included: #72 (Bug 3) observed systemd
# 252 failing to exec with ENOENT under `read-only`, because `uv tool install`
# leaves a symlink chain entirely inside /home
# (~/.local/bin -> ~/.local/share/uv/tools/...) and the sandbox breaks the
# chain rather than the final file. So the allowed set is spelled as the
# negative: anything that is not explicitly off counts as hiding /home.
_PROTECT_HOME_OFF = frozenset({"no", "false", "0", "off"})

# systemd.service(5): `ExecStart=` accepts `-` (ignore failure), `@` (pass a
# custom argv[0]), `:` (no variable expansion) and `+`/`!`/`!!` (privilege
# modifiers) before the path, in any order.
_EXEC_PREFIXES = "-@:+!"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Path:
    tmp_path = tmp_path_factory.mktemp("sweep")
    config = dict(_CONFIG)
    config["scaffold"] = {**_CONFIG["scaffold"], "output_dir": str(tmp_path / "output")}
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(config))
    ScaffoldRenderer(FraisierConfig(path)).render()
    return tmp_path / "output"


def _units(rendered: Path) -> list[Path]:
    """Every rendered `.service`, wherever in the tree it landed."""
    units = sorted(rendered.rglob("*.service"))
    assert units, "the sweep rendered no service units — the fixture is wrong"
    return units


def _directive(text: str, key: str) -> list[str]:
    """Values assigned to *key*, in file order."""
    return [
        line.strip().split("=", 1)[1]
        for line in text.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def _exec_binary(value: str) -> str:
    """The executable path out of an `ExecStart=` value, prefixes stripped."""
    return value.lstrip(_EXEC_PREFIXES).split()[0] if value.strip() else ""


def _without_comments(template: str) -> str:
    """Template text with Jinja comments removed.

    A rule about what templates *read* must not fire on a comment explaining
    why they no longer read it — otherwise documenting the fix reintroduces
    the failure.
    """
    return re.sub(r"\{#.*?#\}", "", template, flags=re.DOTALL)


class TestSandboxDoesNotHideTheBinary:
    """A unit must not hide the filesystem its own ExecStart lives on."""

    def test_no_unit_pairs_protect_home_with_a_home_execstart(self, rendered):
        """`ProtectHome=` plus `ExecStart=/home/…` fails to exec, every time.

        fraisier installs as a uv tool and runs from
        `/home/{deploy_user}/.local/bin/fraisier`. `deploy-service.j2` has
        carried a comment saying so since #72; `deploy-checker.service` and
        `restore-staging.service` contradicted it and would have failed on
        exec the first time anyone enabled them (#341).

        Note the app's own service unit sets `ProtectHome=true` and is
        correct to — its `ExecStart` is under `app_path`. The rule is about
        a unit hiding *its own* binary, not about the directive.
        """
        offenders = []
        for unit in _units(rendered):
            text = unit.read_text()
            protections = _directive(text, "ProtectHome")
            if not any(v.strip().lower() not in _PROTECT_HOME_OFF for v in protections):
                continue
            for value in _directive(text, "ExecStart"):
                binary = _exec_binary(value)
                if binary.startswith("/home/"):
                    offenders.append(f"{unit.name}: ProtectHome= hides {binary}")

        assert not offenders, (
            "units that cannot exec their own ExecStart:\n" + "\n".join(offenders)
        )


@pytest.fixture(scope="module")
def rendered_with_default_output_dir(tmp_path_factory) -> Path:
    """The same tree, rendered under the *default* `scaffold.output_dir`.

    That default is `scripts/generated` — relative, resolved against the
    operator's CWD. Every other fixture here pins an absolute `output_dir`
    because a test has to render somewhere it owns, and that pinning is
    exactly what hides a unit interpolating the render path into an
    `ExecStart=`. This one does not pin it.
    """
    import os

    tmp_path = tmp_path_factory.mktemp("default-output-dir")
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(_CONFIG))
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        ScaffoldRenderer(FraisierConfig(path)).render()
    finally:
        os.chdir(previous)
    return tmp_path / "scripts" / "generated"


class TestExecStartIsRunnable:
    """systemd resolves nothing for you — a relative path is a load failure."""

    def test_every_execstart_is_an_absolute_path(
        self, rendered_with_default_output_dir
    ):
        """`backup.service` rendered `ExecStart=scripts/generated/backup.sh`.

        systemd refuses to load a unit whose `ExecStart=` is not absolute, so
        the whole `backup.timer` → `backup.service` chain was dead on a
        default config before enablement ever came into it (#341).
        """
        offenders = []
        for unit in _units(rendered_with_default_output_dir):
            for value in _directive(unit.read_text(), "ExecStart"):
                binary = _exec_binary(value)
                if not binary.startswith("/"):
                    offenders.append(f"{unit.name}: ExecStart={binary!r}")

        assert not offenders, "ExecStart= values systemd will not load:\n" + "\n".join(
            offenders
        )

    def test_no_execstart_points_into_the_local_render_dir(self, rendered):
        """An absolute `output_dir` turns the same bug into a subtler one.

        `scaffold.output_dir` is where `fraisier scaffold` writes for local
        review; the host reads its tree from `scaffold.state_dir` (#283).
        A unit built from the former is either unloadable (relative default)
        or points at a directory that exists only on the machine that ran
        `scaffold` — same defect, and only the second survives a fixture that
        pins an absolute path. `backup.service.j2` was the last template
        still reading it.
        """
        offenders = []
        for unit in _units(rendered):
            for value in _directive(unit.read_text(), "ExecStart"):
                binary = _exec_binary(value)
                if binary.startswith(str(rendered)):
                    offenders.append(f"{unit.name}: ExecStart={binary!r}")

        assert not offenders, (
            "ExecStart= values pointing into the local render dir:\n"
            + "\n".join(offenders)
        )

    def test_no_template_reads_scaffold_output_dir(self):
        """The boundary, stated where it can be enforced rather than inferred.

        #283 moved every server-side path onto `scaffold.state_dir` and this
        one template was missed, which is only findable by rendering and
        reading — nothing failed. A template reading `scaffold.output_dir` is
        building a host artifact out of a local path, so the honest rule is
        that no template may read it at all, not that this one no longer does.
        """
        import fraisier.scaffold

        templates = Path(fraisier.scaffold.__file__).parent / "templates"
        offenders = [
            str(path.relative_to(templates))
            for path in sorted(templates.rglob("*.j2"))
            if "scaffold.output_dir" in _without_comments(path.read_text())
        ]

        assert not offenders, (
            "templates building host artifacts from the local render path "
            "(use scaffold_state_dir):\n" + "\n".join(offenders)
        )

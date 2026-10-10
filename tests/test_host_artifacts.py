"""The Python host gate picks exactly what install.sh's bash gate installs (#433).

The root helper and the root policy decide which artifacts belong to this host
without running install.sh. Two gates that can disagree are #336 waiting to
happen again, so this pins the Python one to the bash one by execution: for
every host in the golden install-plan matrix, the ``(source, destination)``
pairs ``host_artifacts`` returns are the ones install.sh copies.
"""

from __future__ import annotations

import json
import re

import pytest

from fraisier.scaffold.artifacts import host_artifacts
from tests.test_host_scope_gate import GLOBAL_DECLARATION
from tests.test_install_plan_golden import MATRIX, _install_plan

# A global `environments:` declaration binds every fraise using the name: the
# `*:env` arm of the gate, which the golden matrix does not reach.
CASES = [*MATRIX, ("global_declaration", GLOBAL_DECLARATION, "shared")]

_COPY = re.compile(r"^sudo (?:cp|install -m 0440) \$SCAFFOLD/(\S+) (\S+)$")


def _copied(plan: list[str]) -> set[tuple[str, str]]:
    return {(m[1], m[2]) for line in plan if (m := _COPY.match(line))}


@pytest.mark.parametrize(
    ("label", "yaml_text", "hostname"), CASES, ids=[c[0] for c in CASES]
)
def test_host_artifacts_match_what_install_sh_copies(
    tmp_path, label, yaml_text, hostname
):
    plan = _install_plan(tmp_path, yaml_text, hostname)
    payload = json.loads(
        (tmp_path / "generated" / "artifact-manifest.json").read_text()
    )

    chosen = host_artifacts(payload, hostname)

    assert chosen is not None, label
    picked = {(a["source"], a["destination"]) for a in chosen}
    assert picked == _copied(plan), label


def test_an_unregistered_host_gets_nothing(tmp_path):
    _label, yaml_text, hostname = MATRIX[0]
    _install_plan(tmp_path, yaml_text, hostname)
    payload = json.loads(
        (tmp_path / "generated" / "artifact-manifest.json").read_text()
    )
    assert host_artifacts(payload, "nobody-registered-this") is None

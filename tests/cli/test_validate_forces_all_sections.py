"""Tests that ``fraisier validate`` forces a full Stage-2 traversal (#220).

After the lazy-load split, ``FraisierConfig.__init__`` no longer
catches deep section errors. ``fraisier validate`` compensates by
explicitly accessing every section so it remains the "one command to
surface every problem at once" entry point.
"""

from __future__ import annotations

from click.testing import CliRunner

from fraisier.cli.main import main


def _write(tmp_path, content):
    path = tmp_path / "fraises.yaml"
    path.write_text(content)
    return path


def test_validate_surfaces_all_section_errors(tmp_path):
    """A config broken in TWO sections must surface BOTH errors."""
    cfg = _write(
        tmp_path,
        """
git:
  provider: github
notifications:
  on_failure:
    - type: fax_machine
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
        health_check:
          timeout: "not-a-number"
""",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "validate"])

    # Non-zero exit — there ARE errors.
    assert result.exit_code != 0, f"expected failure, got:\n{result.output}"

    # Both errors must appear in the output.
    assert "fax_machine" in result.output, (
        f"notifications error missing from output:\n{result.output}"
    )
    assert "timeout" in result.output, (
        f"fraise env error missing from output:\n{result.output}"
    )

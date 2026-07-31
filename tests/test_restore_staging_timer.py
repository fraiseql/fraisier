"""The nightly staging restore must fire once, at the advertised hour (#311).

`OnCalendar=` accumulates: systemd resets the trigger list only when the *empty*
string is assigned, so two non-empty assignments give two triggers. The template
carried both `OnCalendar=daily` (00:00) and `OnCalendar=*-*-* 02:00:00` under a
comment promising 2 AM, so it fired twice a day.

That was not merely redundant. On printoptim.dev (2026-07-30) the undocumented
midnight fire landed on an in-flight deploy and killed it — the other half of
that collision is the missing deployment lock (#310).
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {output}
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
"""


@pytest.fixture
def timer_text(tmp_path) -> str:
    p = tmp_path / "fraises.yaml"
    p.write_text(_YAML.format(output=str(tmp_path / "output")))
    ScaffoldRenderer(FraisierConfig(p)).render()
    return (tmp_path / "output" / "systemd" / "restore-staging.timer").read_text()


def _oncalendar_lines(text: str) -> list[str]:
    return [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("OnCalendar=")
    ]


class TestRestoreStagingTimerFiresOnce:
    def test_exactly_one_oncalendar_directive(self, timer_text):
        """Two non-empty OnCalendar= assignments mean two triggers, not an override."""
        lines = _oncalendar_lines(timer_text)

        assert len(lines) == 1, f"timer fires {len(lines)} times a day: {lines}"

    def test_the_single_trigger_is_the_advertised_hour(self, timer_text):
        """The comment promises 2 AM; the directive must agree with it."""
        assert _oncalendar_lines(timer_text) == ["OnCalendar=*-*-* 02:00:00"]

    def test_no_bare_daily_shorthand(self, timer_text):
        """`daily` expands to 00:00 — the midnight fire behind the #310 collision.

        Checks the *directives*, not the raw text: the template comment names
        `OnCalendar=daily` deliberately, to explain the trap it documents.
        """
        assert "OnCalendar=daily" not in _oncalendar_lines(timer_text)

    def test_persistent_is_kept(self, timer_text):
        """A missed run must still catch up; only the extra trigger goes."""
        assert "Persistent=true" in timer_text

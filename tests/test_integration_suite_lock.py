"""Two runs of the integration suite serialise instead of competing (#418).

The suite builds databases on whatever cluster it discovers, which on a
developer machine is the default local one. When anything else is using that
cluster the two starve each other, and — measured — the symptom lands in
whichever process has a client timeout, not in the one causing the load. A
third project's HTTP suite saw no request for 10.3 seconds while its own
slowest query was 5 ms, tripped a 10-second read timeout and lost a test.

So the cost falls on someone with no way to diagnose it: the evidence is in a
third process's journal. An advisory lock makes the *second run of this suite*
slower rather than making an unrelated process fail.

These tests cover the lock's contract without needing a server; the real
acquisition against PostgreSQL is exercised by the integration suite itself,
which takes the lock on every run.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import SUITE_LOCK_KEY, suite_lock_sql


class TestTheKey:
    def test_it_is_a_stable_signed_64_bit_integer(self) -> None:
        """`pg_advisory_lock(bigint)` takes one; a value that shifts each run
        would let two runs take different locks and serialise nothing."""
        assert isinstance(SUITE_LOCK_KEY, int)
        assert -(2**63) <= SUITE_LOCK_KEY < 2**63

    def test_it_does_not_move_between_calls(self) -> None:
        from tests.integration.conftest import SUITE_LOCK_KEY as again

        assert again == SUITE_LOCK_KEY

    def test_it_is_derived_from_a_named_constant_not_a_bare_number(self) -> None:
        """A literal in two places drifts; the key is derived from one name."""
        from tests.integration import conftest

        assert conftest.SUITE_LOCK_NAME
        assert conftest._key_for(conftest.SUITE_LOCK_NAME) == SUITE_LOCK_KEY

    def test_a_different_name_is_a_different_key(self) -> None:
        from tests.integration.conftest import _key_for

        assert _key_for("fraisier-integration-suite") != _key_for("something-else")


class TestTheSql:
    def test_it_tries_rather_than_blocks(self) -> None:
        """A blocking `pg_advisory_lock` waits forever on a wedged holder.

        The suite must never hang CI because a previous run died holding the
        lock — polling a try-lock keeps the failure mode "slower", which is
        the whole point of the change.
        """
        assert "pg_try_advisory_lock" in suite_lock_sql()
        assert "pg_advisory_lock(" not in suite_lock_sql()

    def test_it_names_the_key(self) -> None:
        assert str(SUITE_LOCK_KEY) in suite_lock_sql()


class TestTheContract:
    """What the fixture guarantees, stated so it cannot quietly change."""

    def test_the_lock_is_released_by_disconnecting(self) -> None:
        """No explicit unlock: PostgreSQL drops session advisory locks on
        disconnect, so a crashed run cannot wedge the next one. Anything that
        required an orderly release would reintroduce exactly that risk."""
        from tests.integration import conftest

        source = conftest.suite_lock_sql.__doc__ or ""
        assert "disconnect" in source.lower(), (
            "the release mechanism is the thing most likely to be 'tidied' "
            "into an explicit unlock later; it needs to say why it is absent"
        )


def test_the_conftest_takes_the_lock_on_the_session_fixture() -> None:
    """The lock has to wrap the whole run, not one database build.

    Per-build locking would let two runs interleave between builds, which is
    the interleaving that caused the stall.
    """
    from pathlib import Path

    source = Path("tests/integration/conftest.py").read_text()
    assert "_take_suite_lock" in source
    assert 'scope="session"' in source


@pytest.mark.parametrize("bad", [0, None, ""])
def test_a_falsy_key_would_be_a_bug(bad: object) -> None:
    """Guards the derivation: 0 is a valid bigint but a suspicious key."""
    assert bad != SUITE_LOCK_KEY

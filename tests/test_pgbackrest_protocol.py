"""The wire format of the pgBackRest root helper, and the parsers behind it (#424).

Every fixture under ``tests/fixtures/pgbackrest/`` was **captured** from pgBackRest
2.59.2 and ``pg_lsclusters`` on PostgreSQL 18.6 in the Docker harness, never
composed: the shapes below — ``info`` exiting 0 for a stanza that does not exist,
``pg_lsclusters`` saying ``down,recovery`` for a restored cluster nobody has
started, a restore log that marks unchanged files rather than listing changed
ones — are exactly what hand-written fixtures get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fraisier.pgbackrest_protocol import (
    RequestRejected,
    parse_lsclusters,
    parse_request,
    parse_restore_log,
    render_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pgbackrest"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestRequests:
    @pytest.mark.parametrize("action", ["info", "status", "stop", "start"])
    def test_a_bare_action_is_accepted(self, action: str) -> None:
        request = parse_request(json.dumps({"action": action}))

        assert (request.action, request.set) == (action, None)

    def test_restore_names_the_backup_set_it_was_shown(self) -> None:
        request = parse_request(
            json.dumps(
                {"action": "restore", "set": "20261001-141744F_20261001-141820I"}
            )
        )

        assert request.set == "20261001-141744F_20261001-141820I"

    @pytest.mark.parametrize(
        "label", ["20261001-141744F", "20261001-141744F_20261001-141746D"]
    )
    def test_full_and_differential_labels_are_accepted(self, label: str) -> None:
        assert (
            parse_request(json.dumps({"action": "restore", "set": label})).set == label
        )

    def test_restore_without_a_set_is_refused(self) -> None:
        """ "Whatever is latest" is decided once, by the client, and shown to the operator."""
        with pytest.raises(RequestRejected, match="set"):
            parse_request('{"action": "restore"}')

    @pytest.mark.parametrize(
        "label",
        [
            "latest",
            "../../etc",
            "20261001-141744F; rm -rf /",
            "20261001-141744X",
            "",
            "x" * 200,
        ],
    )
    def test_a_set_that_is_not_a_backup_label_is_refused(self, label: str) -> None:
        with pytest.raises(RequestRejected, match="label"):
            parse_request(json.dumps({"action": "restore", "set": label}))

    def test_a_set_on_anything_but_restore_is_refused(self) -> None:
        with pytest.raises(RequestRejected, match="set"):
            parse_request(json.dumps({"action": "stop", "set": "20261001-141744F"}))

    @pytest.mark.parametrize(
        "action", ["", "shell", "restore --force", "INFO", None, 3]
    )
    def test_an_unknown_action_is_refused(self, action: object) -> None:
        with pytest.raises(RequestRejected, match="action"):
            parse_request(json.dumps({"action": action}))

    @pytest.mark.parametrize(
        "extra",
        [
            {"path": "/var/lib/postgresql/18/prod"},
            {"argv": ["rm", "-rf", "/"]},
            {"command": "pgbackrest"},
            {"cluster": "18/prod"},
            {"stanza": "other"},
            {"fraise": "x", "env": "production"},
        ],
    )
    def test_a_request_can_carry_no_path_argv_or_target(self, extra: dict) -> None:
        """The helper is told *which operation*, and nothing it would have to trust.

        Stanza, repo, cluster and target are baked into its root-owned unit; a
        field here that could name any of them would be the escalation.
        """
        with pytest.raises(RequestRejected, match="unexpected"):
            parse_request(json.dumps({"action": "info", **extra}))

    @pytest.mark.parametrize(
        "raw", ["", "not json", "[]", '"info"', "null", b"\xff\xfe"]
    )
    def test_malformed_input_is_refused_not_raised(self, raw: object) -> None:
        with pytest.raises(RequestRejected):
            parse_request(raw)  # ty: ignore[invalid-argument-type]


class TestResponses:
    def test_one_json_line(self) -> None:
        line = render_response(ok=True, returncode=0, stdout="x")

        assert line.endswith(b"\n")
        assert json.loads(line) == {"ok": True, "returncode": 0, "stdout": "x"}

    def test_an_error_is_ok_false_with_a_message(self) -> None:
        assert json.loads(render_response(ok=False, error="nope")) == {
            "ok": False,
            "error": "nope",
        }


class TestListingClusters:
    def test_an_online_cluster(self) -> None:
        rows = parse_lsclusters(fixture("pg_lsclusters-staging-online.txt"))

        staging = next(r for r in rows if r.name == "staging")
        assert (staging.version, staging.port, staging.status, staging.owner) == (
            "18",
            5433,
            "online",
            "postgres",
        )
        assert staging.datadir == "/var/lib/postgresql/18/staging"
        assert staging.is_online

    def test_a_stopped_cluster(self) -> None:
        rows = parse_lsclusters(fixture("pg_lsclusters-staging-down.txt"))

        staging = next(r for r in rows if r.name == "staging")
        assert staging.status == "down"
        assert not staging.is_online

    def test_down_recovery_is_down(self) -> None:
        """``down,recovery``: restored, never started. Not running — and not "down" either."""
        line = "18 staging 5433 down,recovery postgres /var/lib/postgresql/18/staging /var/log/x.log\n"

        (row,) = parse_lsclusters(line)

        assert row.status == "down,recovery"
        assert not row.is_online

    def test_online_recovery_is_still_online(self) -> None:
        line = "18 staging 5433 online,recovery postgres /var/lib/postgresql/18/staging /var/log/x.log\n"

        (row,) = parse_lsclusters(line)

        assert row.is_online

    def test_blank_and_garbled_lines_are_skipped_not_misread(self) -> None:
        rows = parse_lsclusters(
            "\n  \nnot a cluster line\n" + fixture("pg_lsclusters-staging-down.txt")
        )

        assert [r.name for r in rows] == ["prod", "staging"]


class TestReadingARestore:
    def test_the_label_and_totals_come_off_the_real_log(self) -> None:
        summary = parse_restore_log(fixture("restore-delta-detail.excerpt.log"))

        assert summary.label == "20261001-141744F_20261001-141746I"
        assert summary.restore_size == "30.3MB"
        assert summary.files_total == 1295

    def test_unchanged_files_are_marked_and_not_counted_as_rewritten(self) -> None:
        """A delta restore lists every file; the ones it did not touch say so."""
        summary = parse_restore_log(fixture("restore-delta-detail.excerpt.log"))

        # 6 written, 4 "exists and matches backup", 2 "exists and is zero size";
        # pg_control.pgbackrest.tmp is listed twice in the excerpt.
        assert summary.files_rewritten == 7

    def test_bytes_rewritten_is_an_estimate_from_the_log_sizes(self) -> None:
        summary = parse_restore_log(fixture("restore-delta-detail.excerpt.log"))

        # 16K + 16K + 8K + 8K + 8K + 8K + 8K (the .tmp file twice) = 72 KiB
        assert summary.bytes_rewritten == 72 * 1024
        assert summary.bytes_rewritten_is_estimate

    def test_a_log_without_the_summary_is_an_empty_summary_not_a_guess(self) -> None:
        summary = parse_restore_log("P00 ERROR: [075]: unable to find backup set\n")

        assert summary.label is None
        assert summary.files_total is None
        assert summary.files_rewritten == 0

    @pytest.mark.parametrize(
        ("size", "bytes_"),
        [
            ("0B", 0),
            ("512B", 512),
            ("8KB", 8192),
            ("1.5MB", 1572864),
            ("2GB", 2 * 1024**3),
        ],
    )
    def test_sizes_are_binary_units(self, size: str, bytes_: int) -> None:
        line = f"2026-10-01 14:00:00.000 P01 DETAIL: restore file /x/y ({size}, 1.00%) checksum abc\n"

        assert parse_restore_log(line).bytes_rewritten == bytes_


def arabic_indic(text: str) -> str:
    """*text* with its ASCII digits replaced by Arabic-Indic ones (U+0660..U+0669)."""
    return "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in text)


class TestOnlyAsciiDigitsAreDigits:
    """Python's ``\\d`` matches every Unicode decimal digit, so the patterns say ``[0-9]``.

    These reach a root helper's argv and a root-owned unit file: a label or an
    instant written in Arabic-Indic digits is not one pgBackRest wrote, and is not
    something to pass along.
    """

    def test_a_label_in_non_ascii_digits_is_refused(self) -> None:
        label = arabic_indic("20261001-141744") + "F"

        with pytest.raises(RequestRejected, match="label"):
            parse_request(json.dumps({"action": "restore", "set": label}))

    def test_a_target_in_non_ascii_digits_is_refused_at_validation(self) -> None:
        from fraisier.config.restore_source import TARGET_INSTANT_RE

        assert (
            TARGET_INSTANT_RE.fullmatch(arabic_indic("2026-10-01 14:18:37+00")) is None
        )
        assert TARGET_INSTANT_RE.fullmatch("2026-10-01 14:18:37+00")

    def test_a_cluster_major_in_non_ascii_digits_is_refused(self) -> None:
        from fraisier.config.restore_source import CLUSTER_RE

        assert CLUSTER_RE.fullmatch(arabic_indic("18") + "/staging") is None
        assert CLUSTER_RE.fullmatch("18/staging")


class TestAMalformedLogIsNotACrash:
    def test_a_size_with_two_dots_does_not_raise(self) -> None:
        line = "2026-10-01 14:00:00.000 P01 DETAIL: restore file /x/y (1.2.3KB, 1.00%) checksum abc\n"

        summary = parse_restore_log(line)

        assert summary.files_rewritten == 0  # not a size pgBackRest prints

    def test_a_size_with_no_digits_does_not_raise(self) -> None:
        line = "2026-10-01 14:00:00.000 P01 DETAIL: restore file /x/y (...KB, 1.00%) checksum abc\n"

        assert parse_restore_log(line).files_rewritten == 0

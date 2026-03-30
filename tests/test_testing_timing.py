"""Tests for timing helpers."""

import logging
import time

from fraisier.testing._timing import TimingReport, timed_phase


class TestTimedPhase:
    def test_logs_elapsed_time(self, caplog):
        logger = logging.getLogger("fraisier.testing")
        with (
            caplog.at_level(logging.INFO, logger="fraisier.testing"),
            timed_phase("hash check", logger),
        ):
            pass

        assert "hash check" in caplog.text
        assert "ms" in caplog.text

    def test_returns_elapsed_ms(self):
        logger = logging.getLogger("fraisier.testing")
        with timed_phase("build", logger) as elapsed:
            time.sleep(0.01)

        assert elapsed.ms >= 10


class TestTimingReport:
    def test_record_and_total(self):
        report = TimingReport()
        report.record("hash_check", 50)
        report.record("build", 3000)
        report.record("template_create", 200)

        assert report.total_ms == 3250
        assert len(report.phases) == 3
        assert report.phases["build"] == 3000

    def test_summary_string(self):
        report = TimingReport()
        report.record("hash_check", 50)
        report.record("build", 3000)

        summary = report.summary()
        assert "hash_check" in summary
        assert "build" in summary
        assert "3050" in summary

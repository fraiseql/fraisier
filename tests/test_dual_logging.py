"""Tests for dual logging (stderr + file with graceful fallback)."""

import logging

from fraisier.logging import setup_logging


class TestSetupLogging:
    """Test the setup_logging function for dual output."""

    def setup_method(self):
        """Reset fraisier logger between tests."""
        logger = logging.getLogger("fraisier")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)

    def test_produces_output_on_stderr(self, capsys):
        """setup_logging() must add a handler that writes to stderr."""
        setup_logging("myfraise")
        logger = logging.getLogger("fraisier")
        logger.info("hello stderr")

        captured = capsys.readouterr()
        assert "hello stderr" in captured.err

    def test_writes_to_log_file(self, tmp_path):
        """When log dir is writable, a file handler is added."""
        setup_logging("myfraise", log_dir=tmp_path)
        logger = logging.getLogger("fraisier")
        logger.info("hello file")

        log_file = tmp_path / "myfraise.log"
        assert log_file.exists()
        assert "hello file" in log_file.read_text()

    def test_graceful_fallback_when_dir_not_writable(self, tmp_path, capsys):
        """When log dir is not writable, no crash; stderr still works."""
        bad_dir = tmp_path / "nope"
        bad_dir.mkdir()
        bad_dir.chmod(0o000)

        try:
            # Must not raise
            setup_logging("myfraise", log_dir=bad_dir)
            logger = logging.getLogger("fraisier")
            logger.info("still works")

            captured = capsys.readouterr()
            assert "still works" in captured.err
        finally:
            bad_dir.chmod(0o755)

    def test_no_file_handler_on_fallback(self, tmp_path):
        """On fallback, there should be no file handler."""
        bad_dir = tmp_path / "nope"
        bad_dir.mkdir()
        bad_dir.chmod(0o000)

        try:
            setup_logging("myfraise", log_dir=bad_dir)
            logger = logging.getLogger("fraisier")
            file_handlers = [
                h for h in logger.handlers if isinstance(h, logging.FileHandler)
            ]
            assert len(file_handlers) == 0
        finally:
            bad_dir.chmod(0o755)

    def test_default_level_is_info(self):
        """Default log level should be INFO."""
        setup_logging("myfraise")
        logger = logging.getLogger("fraisier")
        assert logger.level == logging.INFO

    def test_custom_level(self):
        """Log level can be overridden."""
        setup_logging("myfraise", level="DEBUG")
        logger = logging.getLogger("fraisier")
        assert logger.level == logging.DEBUG

    def test_stderr_handler_goes_to_stderr_not_stdout(self, capsys):
        """Handler must target stderr (for systemd journal capture)."""
        setup_logging("myfraise")
        logger = logging.getLogger("fraisier")
        logger.info("stderr check")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "stderr check" in captured.err

    def test_json_format_on_file(self, tmp_path):
        """File handler should use JSON formatting."""
        import json

        setup_logging("myfraise", log_dir=tmp_path)
        logger = logging.getLogger("fraisier")
        logger.info("json test")

        log_file = tmp_path / "myfraise.log"
        line = log_file.read_text().strip()
        parsed = json.loads(line)
        assert parsed["message"] == "json test"
        assert parsed["level"] == "INFO"

    def test_clears_existing_handlers(self):
        """setup_logging() should clear existing handlers to avoid duplicates."""
        logger = logging.getLogger("fraisier")
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.StreamHandler())
        assert len(logger.handlers) == 2

        setup_logging("myfraise")
        # Should have exactly 1 handler (stderr), not 3
        stderr_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert len(stderr_handlers) == 1

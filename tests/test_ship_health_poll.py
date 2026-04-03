"""Tests for ship health polling functionality."""

from unittest.mock import MagicMock, patch

from fraisier.ship.health_poll import PollResult, poll_health_for_version


class TestPollHealthForVersion:
    """Test health polling functionality."""

    def test_poll_success_when_version_matches(self):
        """Poll returns success when health endpoint returns matching version."""
        health_url = "http://example.com/health"
        expected_version = "1.2.3"

        # Mock httpx.get to return expected version
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "version": expected_version,
            "status": "healthy",
        }

        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = poll_health_for_version(
                health_url=health_url,
                expected_version=expected_version,
                timeout=10,
                interval=1,
                console_output=False,
            )

            assert result.success is True
            assert result.final_version == expected_version
            assert result.attempts == 1
            assert result.elapsed_seconds < 2  # Should complete quickly
            mock_get.assert_called_once_with(health_url, timeout=5)

    def test_poll_failure_on_timeout(self):
        """Poll returns failure when version never matches within timeout."""
        health_url = "http://example.com/health"
        expected_version = "1.2.3"

        # Mock httpx.get to return wrong version
        mock_response = MagicMock()
        mock_response.json.return_value = {"version": "1.2.2", "status": "healthy"}

        with patch("httpx.get", return_value=mock_response):
            result = poll_health_for_version(
                health_url=health_url,
                expected_version=expected_version,
                timeout=2,  # Short timeout
                interval=1,
                console_output=False,
            )

            assert result.success is False
            assert result.final_version == "1.2.2"
            assert result.attempts >= 1
            assert 1 <= result.elapsed_seconds <= 3

    def test_poll_handles_http_errors(self):
        """Poll handles HTTP errors gracefully and continues."""
        health_url = "http://example.com/health"
        expected_version = "1.2.3"

        # Mock httpx.get to fail first, then succeed
        mock_response_fail = MagicMock()
        mock_response_fail.raise_for_status.side_effect = Exception("HTTP 500")

        mock_response_success = MagicMock()
        mock_response_success.json.return_value = {"version": expected_version}

        with patch(
            "httpx.get", side_effect=[mock_response_fail, mock_response_success]
        ):
            result = poll_health_for_version(
                health_url=health_url,
                expected_version=expected_version,
                timeout=10,
                interval=1,
                console_output=False,
            )

            assert result.success is True
            assert result.final_version == expected_version
            assert result.attempts == 2

    def test_extract_version_from_health_response(self):
        """Test version extraction from various health response formats."""
        from fraisier.ship.health_poll import _extract_version_from_health_response

        # Test various field names
        assert _extract_version_from_health_response({"version": "1.0.0"}) == "1.0.0"
        assert (
            _extract_version_from_health_response({"app_version": "2.0.0"}) == "2.0.0"
        )
        assert _extract_version_from_health_response({"build": "abc123"}) == "abc123"
        assert (
            _extract_version_from_health_response({"build_version": "def456"})
            == "def456"
        )

        # Test missing version
        assert _extract_version_from_health_response({"status": "healthy"}) is None

        # Test empty response
        assert _extract_version_from_health_response({}) is None

    def test_poll_result_dataclass(self):
        """Test PollResult dataclass creation."""
        result = PollResult(
            success=True,
            final_version="1.2.3",
            elapsed_seconds=45.5,
            attempts=5,
        )

        assert result.success is True
        assert result.final_version == "1.2.3"
        assert result.elapsed_seconds == 45.5
        assert result.attempts == 5

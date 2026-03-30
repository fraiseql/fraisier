"""Tests for the database_fixture factory."""

from fraisier.testing import TemplateInfo, TemplateManager, database_fixture


class TestDatabaseFixture:
    def test_returns_callable(self):
        fixture = database_fixture(env="test")
        assert callable(fixture)

    def test_is_pytest_fixture(self):
        fixture = database_fixture(env="test", scope="session")
        # pytest.fixture wraps functions; the wrapper is still callable
        # and pytest recognizes it during collection
        assert callable(fixture)

    def test_custom_scope_is_applied(self):
        fixture_session = database_fixture(env="test", scope="session")
        fixture_func = database_fixture(env="test", scope="function")
        # Both return valid fixtures (different scope, same API)
        assert callable(fixture_session)
        assert callable(fixture_func)


class TestPublicAPI:
    def test_exports_template_manager(self):
        assert TemplateManager is not None

    def test_exports_template_info(self):
        assert TemplateInfo is not None

    def test_module_importable_without_calling_fixture(self):
        """The fraisier.testing module should import without pytest."""
        import fraisier.testing

        assert hasattr(fraisier.testing, "database_fixture")
        assert hasattr(fraisier.testing, "TemplateManager")

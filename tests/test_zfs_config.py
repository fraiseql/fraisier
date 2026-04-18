"""Tests for ZFS configuration parsing and validation."""

import pytest

from fraisier.config._validation import _validate_zfs_config
from fraisier.config.schema import ZFSConfig
from fraisier.errors import ValidationError


class TestZFSConfig:
    """Test ZFS configuration parsing and validation."""

    def test_zfs_config_required_fields(self):
        """Test ZFS config with all required fields."""
        config_data = {
            "enabled": True,
            "pool": "zroot",
            "data_dataset": "pgsql/data",
        }

        config = ZFSConfig(**config_data)

        assert config.enabled is True
        assert config.pool == "zroot"
        assert config.data_dataset == "pgsql/data"
        assert config.snapshot_prefix == "snap"  # default
        assert config.clone_prefix == "clone"  # default
        assert config.max_snapshot_age_days == 7  # default
        assert config.snapshot_retention == 10  # default

    def test_zfs_config_all_fields(self):
        """Test ZFS config with all optional fields set."""
        config_data = {
            "enabled": True,
            "pool": "tank",
            "data_dataset": "app/data",
            "snapshot_prefix": "prod_backup_",
            "clone_prefix": "deploy_",
            "max_snapshot_age_days": 14,
            "snapshot_retention": 20,
        }

        config = ZFSConfig(**config_data)

        assert config.enabled is True
        assert config.pool == "tank"
        assert config.data_dataset == "app/data"
        assert config.snapshot_prefix == "prod_backup_"
        assert config.clone_prefix == "deploy_"
        assert config.max_snapshot_age_days == 14
        assert config.snapshot_retention == 20

    def test_zfs_config_defaults(self):
        """Test ZFS config defaults when not specified."""
        config_data = {
            "enabled": False,
            "pool": "zroot",
            "data_dataset": "data",
        }

        config = ZFSConfig(**config_data)

        assert config.enabled is False
        assert config.pool == "zroot"
        assert config.data_dataset == "data"
        assert config.snapshot_prefix == "snap"
        assert config.clone_prefix == "clone"
        assert config.max_snapshot_age_days == 7
        assert config.snapshot_retention == 10

    def test_zfs_config_validation_required_fields(self):
        """Test validation of required fields."""
        # Missing pool
        with pytest.raises(ValidationError, match=r"pool.*required"):
            ZFSConfig(enabled=True, data_dataset="data")

        # Missing data_dataset
        with pytest.raises(ValidationError, match=r"data_dataset.*required"):
            ZFSConfig(enabled=True, pool="zroot")

    def test_zfs_config_validation_prefix_format(self):
        """Test validation of prefix naming rules."""
        # Valid prefixes
        valid_prefixes = ["snap", "prod_", "backup_123", "my_snapshots_"]
        for prefix in valid_prefixes:
            config = ZFSConfig(
                enabled=True, pool="zroot", data_dataset="data", snapshot_prefix=prefix
            )
            assert config.snapshot_prefix == prefix

        # Invalid prefixes
        invalid_prefixes = ["snap!", "snap space", "snap@domain", "123start"]
        for prefix in invalid_prefixes:
            with pytest.raises(
                ValidationError, match=r"prefix.*alphanumeric.*underscore"
            ):
                ZFSConfig(
                    enabled=True,
                    pool="zroot",
                    data_dataset="data",
                    snapshot_prefix=prefix,
                )

    def test_zfs_config_validation_numeric_fields(self):
        """Test validation of numeric fields."""
        # Valid values
        config = ZFSConfig(
            enabled=True,
            pool="zroot",
            data_dataset="data",
            max_snapshot_age_days=30,
            snapshot_retention=50,
        )
        assert config.max_snapshot_age_days == 30
        assert config.snapshot_retention == 50

        # Invalid negative values
        with pytest.raises(ValidationError, match=r"age.*positive"):
            ZFSConfig(
                enabled=True,
                pool="zroot",
                data_dataset="data",
                max_snapshot_age_days=-1,
            )

        with pytest.raises(ValidationError, match=r"retention.*positive"):
            ZFSConfig(
                enabled=True, pool="zroot", data_dataset="data", snapshot_retention=0
            )

    def test_zfs_config_computed_properties(self):
        """Test computed properties for full dataset paths."""
        config = ZFSConfig(enabled=True, pool="zroot", data_dataset="app/data")

        assert config.full_dataset_path == "zroot/app/data"
        assert config.pool_path == "zroot"

    def test_zfs_config_disabled_behavior(self):
        """Test behavior when ZFS is disabled."""
        config = ZFSConfig(enabled=False, pool="zroot", data_dataset="data")

        assert config.enabled is False
        # Other fields should still be accessible
        assert config.pool == "zroot"

    def test_zfs_config_validation_function(self):
        """Test the ZFS config validation function."""
        # Valid config
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "app/data",
                "snapshot_prefix": "prod_",
                "max_snapshot_age_days": 14,
            },
        )
        assert errors == []

    def test_zfs_config_validation_disabled(self):
        """Test validation when ZFS is disabled."""
        # Should not validate required fields when disabled
        errors = _validate_zfs_config("my_app", {"enabled": False})
        assert errors == []

    def test_zfs_config_validation_function_required_fields(self):
        """Test validation of required fields when enabled."""
        # Missing pool
        errors = _validate_zfs_config(
            "my_app", {"enabled": True, "data_dataset": "app/data"}
        )
        assert len(errors) == 1
        assert "pool is required" in errors[0]

        # Missing data_dataset
        errors = _validate_zfs_config("my_app", {"enabled": True, "pool": "zroot"})
        assert len(errors) == 1
        assert "data_dataset is required" in errors[0]

    def test_zfs_config_validation_types(self):
        """Test validation of field types."""
        # enabled not boolean
        errors = _validate_zfs_config(
            "my_app", {"enabled": "yes", "pool": "zroot", "data_dataset": "data"}
        )
        assert len(errors) == 1
        assert "enabled must be a boolean" in errors[0]

        # pool not string
        errors = _validate_zfs_config(
            "my_app", {"enabled": True, "pool": 123, "data_dataset": "data"}
        )
        assert len(errors) == 1
        assert "pool must be a string" in errors[0]

        # max_snapshot_age_days not integer
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "data",
                "max_snapshot_age_days": "14",
            },
        )
        assert len(errors) == 1
        assert "max_snapshot_age_days must be an integer" in errors[0]

    def test_zfs_config_validation_prefixes(self):
        """Test validation of prefix formats."""
        # Invalid snapshot_prefix
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "data",
                "snapshot_prefix": "123invalid",
            },
        )
        assert len(errors) == 1
        assert "snapshot_prefix" in errors[0]
        assert "start with a letter or underscore" in errors[0]

        # Invalid clone_prefix
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "data",
                "clone_prefix": "invalid@prefix",
            },
        )
        assert len(errors) == 1
        assert "clone_prefix" in errors[0]

    def test_zfs_config_validation_numeric_ranges(self):
        """Test validation of numeric field ranges."""
        # Negative age
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "data",
                "max_snapshot_age_days": -1,
            },
        )
        assert len(errors) == 1
        assert "must be positive" in errors[0]

        # Zero retention
        errors = _validate_zfs_config(
            "my_app",
            {
                "enabled": True,
                "pool": "zroot",
                "data_dataset": "data",
                "snapshot_retention": 0,
            },
        )
        assert len(errors) == 1
        assert "must be positive" in errors[0]

    def test_zfs_config_validation_not_dict(self):
        """Test validation when zfs config is not a dict."""
        errors = _validate_zfs_config("my_app", "not a dict")
        assert len(errors) == 1
        assert "must be a mapping" in errors[0]

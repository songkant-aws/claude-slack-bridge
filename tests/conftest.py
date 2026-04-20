from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Temporary config directory for tests."""
    config_dir = tmp_path / "slack-bridge"
    config_dir.mkdir()
    return config_dir

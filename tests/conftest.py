import pytest

from lxreview.paths import Paths


@pytest.fixture
def paths(tmp_path):
    value = Paths(tmp_path / "lxreview")
    value.ensure()
    return value

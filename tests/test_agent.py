import pytest

from dropconvert.agent import plural_files


@pytest.mark.parametrize("n, text", [(0, "0 files"), (1, "1 file"), (2, "2 files"), (21, "21 files")])
def test_plural(n, text):
    assert plural_files(n) == text

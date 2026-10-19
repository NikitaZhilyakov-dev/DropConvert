import pytest

from dropconvert.agent import plural_files


@pytest.mark.parametrize("n, text", [(1, "1 файл"), (3, "3 файла"), (5, "5 файлов"),
                                     (11, "11 файлов"), (21, "21 файл"), (22, "22 файла")])
def test_plural(n, text):
    assert plural_files(n) == text

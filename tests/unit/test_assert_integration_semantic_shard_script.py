from __future__ import annotations

import runpy
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "assert_integration_semantic_shard.py"
)
SCRIPT_GLOBALS = runpy.run_path(str(SCRIPT_PATH))

_load_marked_tests = SCRIPT_GLOBALS["_load_marked_tests"]


def test_load_marked_tests_includes_top_level_functions(tmp_path: Path) -> None:
    test_file = tmp_path / "test_top_level.py"
    test_file.write_text(
        """
import pytest

@pytest.mark.integration_semantic
def test_top_level():
    pass
""",
        encoding="utf-8",
    )

    assert _load_marked_tests(test_file) == {"test_top_level"}


def test_load_marked_tests_includes_test_class_methods(tmp_path: Path) -> None:
    test_file = tmp_path / "test_class_method.py"
    test_file.write_text(
        """
import pytest

class TestSemanticShard:
    @pytest.mark.integration_semantic
    def test_from_class(self):
        pass
""",
        encoding="utf-8",
    )

    assert _load_marked_tests(test_file) == {"TestSemanticShard::test_from_class"}


def test_load_marked_tests_excludes_nested_function_definitions(tmp_path: Path) -> None:
    test_file = tmp_path / "test_nested.py"
    test_file.write_text(
        """
import pytest

def test_wrapper():
    @pytest.mark.integration_semantic
    def test_nested():
        pass

@pytest.mark.integration_semantic
def test_visible():
    pass
""",
        encoding="utf-8",
    )

    assert _load_marked_tests(test_file) == {"test_visible"}


def test_load_marked_tests_distinguishes_same_named_methods_across_classes(tmp_path: Path) -> None:
    test_file = tmp_path / "test_duplicate_method_names.py"
    test_file.write_text(
        """
import pytest

class TestOne:
    @pytest.mark.integration_semantic
    def test_contract(self):
        pass

class TestTwo:
    @pytest.mark.integration_semantic
    def test_contract(self):
        pass
""",
        encoding="utf-8",
    )

    assert _load_marked_tests(test_file) == {
        "TestOne::test_contract",
        "TestTwo::test_contract",
    }

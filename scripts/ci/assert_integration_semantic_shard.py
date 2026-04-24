from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator


EXPECTED_SHARD_TESTS: dict[str, str] = {
    "tests/integration/test_client_reconnect.py": "test_tcp_client_reconnects_after_server_appears",
    "tests/integration/test_heartbeat_integration.py": "test_heartbeat_restarts_after_reconnect",
    "tests/integration/test_on_event_payload_contract.py": "test_on_event_bytes_received_event_contains_payload_bytes",
}


def _has_integration_semantic_marker(function_node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for decorator in function_node.decorator_list:
        if not isinstance(decorator, ast.Attribute):
            continue
        if decorator.attr != "integration_semantic":
            continue
        mark_attribute = decorator.value
        if not isinstance(mark_attribute, ast.Attribute):
            continue
        if mark_attribute.attr != "mark":
            continue
        pytest_name = mark_attribute.value
        if isinstance(pytest_name, ast.Name) and pytest_name.id == "pytest":
            return True
    return False


def _load_marked_tests(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    marked: set[str] = set()
    for test_id, node in _iter_collectable_function_nodes(module):
        if _has_integration_semantic_marker(node):
            marked.add(test_id)
    return marked


def _iter_collectable_function_nodes(
    module: ast.Module,
) -> Iterator[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    stack: list[tuple[ast.Module | ast.ClassDef, tuple[str, ...]]] = [(module, ())]
    while stack:
        node, class_path = stack.pop()
        for child in node.body:
            if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if class_path:
                    yield ("::".join((*class_path, child.name)), child)
                else:
                    yield (child.name, child)
            elif isinstance(child, ast.ClassDef):
                stack.append((child, (*class_path, child.name)))


def main() -> None:
    expected_total = len(EXPECTED_SHARD_TESTS)
    observed_total = 0

    for file_path_str, expected_test_name in EXPECTED_SHARD_TESTS.items():
        file_path = Path(file_path_str)
        marked_tests = _load_marked_tests(file_path)
        if expected_test_name not in marked_tests:
            raise AssertionError(
                f"required integration_semantic shard test missing marker: {file_path_str}::{expected_test_name}"
            )
        observed_total += 1

    all_marked_tests: list[str] = []
    for integration_test_file in sorted(Path("tests/integration").glob("test_*.py")):
        marked = _load_marked_tests(integration_test_file)
        all_marked_tests.extend(
            f"{integration_test_file}::{test_name}" for test_name in sorted(marked)
        )

    if len(all_marked_tests) != expected_total:
        raise AssertionError(
            "integration_semantic shard drift detected: "
            f"expected exactly {expected_total} marked tests, found {len(all_marked_tests)}: {all_marked_tests}"
        )

    print(
        "integration_semantic shard contract verified: "
        f"{observed_total} required tests marked; total marked tests={len(all_marked_tests)}"
    )


if __name__ == "__main__":
    main()

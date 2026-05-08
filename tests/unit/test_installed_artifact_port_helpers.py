from __future__ import annotations

import importlib.util
import ast
import inspect
import socket
from pathlib import Path
from types import ModuleType

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_script(script_name: str) -> ModuleType:
    script_path = _repo_root() / "scripts" / "ci" / script_name
    spec = importlib.util.spec_from_file_location(f"_aionetx_{script_path.stem}", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_ast(script_name: str) -> ast.Module:
    script_path = _repo_root() / "scripts" / "ci" / script_name
    return ast.parse(script_path.read_text(encoding="utf-8"))


def _tcp_client_settings_keywords(script_name: str, function_name: str) -> set[str]:
    module = _script_ast(script_name)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return {
                keyword.arg
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "TcpClientSettings"
                for keyword in child.keywords
                if keyword.arg is not None
            }
    raise AssertionError(f"{function_name} not found in {script_name}")


def _assert_tcp_port_is_held(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        with pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))


def test_smoke_reconnect_port_helper_holds_port_until_release() -> None:
    smoke = _load_script("installed_artifact_smoke.py")

    with smoke._reserved_tcp_failure_port() as port:
        _assert_tcp_port_is_held(port)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_semantic_reconnect_port_helper_holds_port_until_release() -> None:
    semantic = _load_script("installed_artifact_semantic_minisuite.py")

    with semantic._reserved_tcp_failure_port() as port:
        _assert_tcp_port_is_held(port)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_released_port_helpers_document_public_api_constraint() -> None:
    expected_helpers = {
        "installed_artifact_smoke.py": (
            "_released_tcp_listener_port",
            "_released_udp_receiver_port",
        ),
        "installed_artifact_semantic_minisuite.py": ("_released_tcp_listener_port",),
    }

    for script_name, helper_names in expected_helpers.items():
        module = _load_script(script_name)
        for helper_name in helper_names:
            doc = inspect.getdoc(getattr(module, helper_name))
            assert doc is not None
            assert "intentional" in doc
            assert "does not accept a pre-bound socket" in doc


def test_installed_artifact_scripts_do_not_keep_generic_unused_port_helpers() -> None:
    for script_name in (
        "installed_artifact_smoke.py",
        "installed_artifact_semantic_minisuite.py",
    ):
        module = _load_script(script_name)
        helper_names = {
            name
            for name in vars(module)
            if name.startswith("_get_unused_tcp_port") or name.startswith("_get_unused_udp_port")
        }
        assert helper_names == set()


def test_reconnect_smokes_bound_connect_attempt_duration() -> None:
    reconnect_checks = {
        "installed_artifact_smoke.py": "_run_reconnect_smoke",
        "installed_artifact_semantic_minisuite.py": "_assert_reconnect_failure_and_recovery",
    }

    for script_name, function_name in reconnect_checks.items():
        keywords = _tcp_client_settings_keywords(script_name, function_name)
        assert "connect_timeout_seconds" in keywords

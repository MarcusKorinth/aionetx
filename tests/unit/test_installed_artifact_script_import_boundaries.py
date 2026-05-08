from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(script_name: str) -> ModuleType:
    script_path = REPO_ROOT / "scripts" / "ci" / script_name
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root_imported_symbols(script_path: Path) -> set[str]:
    module = ast.parse(script_path.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "aionetx":
            symbols.update(alias.name for alias in node.names)
    return symbols


def test_installed_artifact_scripts_only_import_curated_root_symbols() -> None:
    import aionetx

    scripts = [
        REPO_ROOT / "scripts" / "ci" / "installed_artifact_smoke.py",
        REPO_ROOT / "scripts" / "ci" / "installed_artifact_semantic_minisuite.py",
    ]

    allowed_root_symbols = set(aionetx.PUBLIC_API)
    for script in scripts:
        imported_from_root = _root_imported_symbols(script)
        assert imported_from_root.issubset(allowed_root_symbols), (
            f"{script.relative_to(REPO_ROOT)} imports non-root curated symbols: "
            f"{sorted(imported_from_root - allowed_root_symbols)!r}"
        )


def test_install_single_artifact_can_disable_build_isolation(monkeypatch, tmp_path: Path) -> None:
    script = _load_script("install_single_artifact.py")
    artifact = tmp_path / "aionetx-0.1.0.tar.gz"
    artifact.write_bytes(b"not a real sdist")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        script.sys,
        "argv",
        [
            "install_single_artifact.py",
            str(artifact),
            "--no-deps",
            "--no-build-isolation",
        ],
    )
    monkeypatch.setattr(script.subprocess, "check_call", calls.append)

    script.main()

    assert calls == [
        [
            script.sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            str(artifact),
        ],
    ]

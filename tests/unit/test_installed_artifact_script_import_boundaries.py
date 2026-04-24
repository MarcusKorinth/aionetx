from __future__ import annotations

import ast
from pathlib import Path


def _root_imported_symbols(script_path: Path) -> set[str]:
    module = ast.parse(script_path.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "aionetx":
            symbols.update(alias.name for alias in node.names)
    return symbols


def test_installed_artifact_scripts_only_import_curated_root_symbols() -> None:
    import aionetx

    repo_root = Path(__file__).resolve().parents[2]
    scripts = [
        repo_root / "scripts" / "ci" / "installed_artifact_smoke.py",
        repo_root / "scripts" / "ci" / "installed_artifact_semantic_minisuite.py",
    ]

    allowed_root_symbols = set(aionetx.PUBLIC_API)
    for script in scripts:
        imported_from_root = _root_imported_symbols(script)
        assert imported_from_root.issubset(allowed_root_symbols), (
            f"{script.relative_to(repo_root)} imports non-root curated symbols: "
            f"{sorted(imported_from_root - allowed_root_symbols)!r}"
        )

from __future__ import annotations

import importlib.util
import sys
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_distribution_artifacts.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(SCRIPT_PATH.stem, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wheel(path: Path, metadata: str) -> None:
    dist_info = "aionetx-0.1.0.dist-info"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
        wheel.writestr(f"{dist_info}/RECORD", "")


def _write_sdist(path: Path, metadata: str) -> None:
    root_name = "aionetx-0.1.0"
    with tarfile.open(path, "w:gz") as sdist:
        payload = metadata.encode("utf-8")
        info = tarfile.TarInfo(f"{root_name}/PKG-INFO")
        info.size = len(payload)
        sdist.addfile(info, BytesIO(payload))


def test_distribution_artifact_checker_accepts_matching_wheel_and_sdist(
    monkeypatch, tmp_path: Path
) -> None:
    script = _load_script()
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: aionetx\n"
        "Version: 0.1.0\n"
        "Summary: Reusable raw-byte network layer\n"
        "\n"
    )
    _write_wheel(tmp_path / "aionetx-0.1.0-py3-none-any.whl", metadata)
    _write_sdist(tmp_path / "aionetx-0.1.0.tar.gz", metadata)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_distribution_artifacts.py",
            str(tmp_path / "*"),
            "--expected-name",
            "aionetx",
            "--expected-version",
            "0.1.0",
        ],
    )

    script.main()


def test_distribution_artifact_checker_rejects_version_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    script = _load_script()
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: aionetx\n"
        "Version: 0.2.0\n"
        "Summary: Reusable raw-byte network layer\n"
        "\n"
    )
    _write_wheel(tmp_path / "aionetx-0.2.0-py3-none-any.whl", metadata)
    _write_sdist(tmp_path / "aionetx-0.2.0.tar.gz", metadata)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_distribution_artifacts.py",
            str(tmp_path / "*"),
            "--expected-name",
            "aionetx",
            "--expected-version",
            "0.1.0",
        ],
    )

    try:
        script.main()
    except SystemExit as exc:
        assert str(exc) == "aionetx-0.2.0-py3-none-any.whl: expected Version 0.1.0, got 0.2.0"
    else:  # pragma: no cover - clarity for assertion failure
        raise AssertionError("metadata mismatch was accepted")


def test_distribution_artifact_checker_entrypoint_does_not_wrap_main_return() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "sys.exit(main())" not in script_text

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_SCRIPT = REPO_ROOT / "bench" / "event_delivery_baseline.py"


@pytest.mark.integration
def test_event_delivery_baseline_benchmark_emits_context_and_metrics() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK_SCRIPT),
            "--payload-count",
            "8",
            "--payload-size",
            "32",
            "--max-pending-events",
            "4",
            "--dispatch-mode",
            "background",
            "--backpressure-policy",
            "block",
            "--json",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    payload = json.loads(result.stdout)

    assert payload["benchmark"] == "tcp-event-delivery-baseline"
    assert payload["context"]["python_version"]
    assert payload["context"]["platform"]
    assert payload["settings"] == {
        "dispatch_mode": "background",
        "backpressure_policy": "block",
        "payload_count": 8,
        "payload_size_bytes": 32,
        "max_pending_events": 4,
    }
    assert payload["metrics"]["bytes_sent"] == 256
    assert payload["metrics"]["bytes_received"] == 256
    assert payload["metrics"]["duration_seconds"] > 0.0
    assert payload["metrics"]["throughput_bytes_per_second"] > 0.0
    assert payload["metrics"]["queue_peak"] >= 0
    assert payload["metrics"]["dropped_backpressure_newest_total"] == 0
    assert payload["metrics"]["dropped_backpressure_oldest_total"] == 0

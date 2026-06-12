from __future__ import annotations

from pathlib import Path


def test_unretrieved_deferred_close_future_diagnostic_fails_async_test(pytester) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pytester.syspathinsert(str(repo_root))
    pytester.syspathinsert(str(repo_root / "src"))
    pytester.makeini(
        """
        [pytest]
        asyncio_default_fixture_loop_scope = function
        """
    )
    pytester.makeconftest((repo_root / "tests" / "conftest.py").read_text())
    pytester.makepyfile(
        """
        from __future__ import annotations

        import asyncio
        import gc

        import pytest


        @pytest.mark.asyncio
        async def test_deferred_close_unretrieved_future_exception() -> None:
            loop = asyncio.get_running_loop()
            deferred_close_waiter = loop.create_future()
            deferred_close_waiter.set_exception(
                RuntimeError("deferred-close-publication-failed")
            )
            del deferred_close_waiter
            gc.collect()
            await asyncio.sleep(0)
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*asyncio reported unretrieved Future exception diagnostics*"])

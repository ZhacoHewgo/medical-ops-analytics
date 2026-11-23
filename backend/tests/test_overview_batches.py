import asyncio

from app.api.routers import overview_router as router
from etl.config.source_config import OdsError


def test_shared_snapshot_only_for_same_scope_and_arrival_before_read(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def summarize(load, start, end, scope, **context):
            calls.append(scope)
            if scope is None and calls.count(None) == 1:
                started.set()
                await release.wait()
            return (scope, calls.count(scope))

        monkeypatch.setattr(router, "run_in_threadpool", summarize)
        monkeypatch.setattr(router, "BATCH_WAIT_SECONDS", 0)
        args = (None, None, None)
        first = [asyncio.create_task(router._overview_batch(args, {})) for _ in range(5)]
        await started.wait()
        late = asyncio.create_task(router._overview_batch(args, {}))
        department = await router._overview_batch((None, None, "HN"), {})
        assert department == ("HN", 1)
        release.set()
        assert await asyncio.gather(*first) == [(None, 1)] * 5
        assert await late == (None, 2)
        assert not router._batches

    asyncio.run(scenario())


def test_failed_validation_is_shared_only_in_the_current_batch(monkeypatch):
    async def scenario():
        calls = 0

        async def summarize(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OdsError("N10_SOURCE_STALE")
            return {"new_batch": True}

        monkeypatch.setattr(router, "run_in_threadpool", summarize)
        monkeypatch.setattr(router, "BATCH_WAIT_SECONDS", 0)
        args = (None, None, None)
        for result in await asyncio.gather(router._overview_batch(args, {}), router._overview_batch(args, {}),
                                           return_exceptions=True):
            assert isinstance(result, OdsError) and result.code == "N10_SOURCE_STALE"
        assert calls == 1
        assert await router._overview_batch(args, {}) == {"new_batch": True}
        assert calls == 2 and not router._batches

    asyncio.run(scenario())


def test_verification_fingerprint_rejects_incomplete_sql_digest(monkeypatch):
    from etl.jobs import build_dwd

    class Cursor:
        def execute(self, *args):
            pass

        def fetchall(self):
            return ()

        def fetchone(self):
            return 2, 64, "a" * 64

    monkeypatch.setattr(build_dwd, "_verification_tables",
                        lambda: [("visit", ("visit_id",), ("visit_id",))])
    assert build_dwd._verification_fingerprint(Cursor(), "course", {}, "", {}, b"test") is None

"""The sweeper loop with a fake pool and a fake sweep_once (no database)."""

import asyncio
import logging

import pytest

from agents_history import sweep


async def until(predicate, seconds: float = 2) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0)


async def test_sweeps_at_start_and_on_wake(settings, monkeypatch):
    calls: list[int] = []

    async def fake_sweep_once(pool, *, batch):
        calls.append(batch)
        return 0

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    stop, wake = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(sweep.run_sweeper(object(), settings, stop, wake))
    await until(lambda: len(calls) == 1)
    await asyncio.sleep(0.05)
    assert len(calls) == 1  # the interval is an hour: nothing more until woken
    wake.set()
    await until(lambda: len(calls) == 2)
    assert calls == [settings.sweep_batch, settings.sweep_batch]
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(calls) == 2


async def test_survives_errors(settings, monkeypatch, caplog):
    outcomes = [RuntimeError("boom"), 3]
    calls: list[int] = []

    async def fake_sweep_once(pool, *, batch):
        calls.append(batch)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ticks: list[float] = []
    stop, wake = asyncio.Event(), asyncio.Event()

    async def tick(awaitable, timeout):
        awaitable.close()
        ticks.append(timeout)
        if len(ticks) == 2:
            stop.set()
        raise TimeoutError

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    with caplog.at_level(logging.INFO, logger="agents_history.sweep"):
        await asyncio.wait_for(sweep.run_sweeper(object(), settings, stop, wake, sleep=tick), 2)
    assert len(calls) == 2
    assert ticks == [settings.sweep_interval_seconds] * 2
    assert any(r.levelno == logging.ERROR and "boom" in (r.exc_text or "") for r in caplog.records)
    assert any("deleted 3" in r.getMessage() for r in caplog.records)


async def test_stop_before_start_sweeps_nothing(settings, monkeypatch):
    async def fake_sweep_once(pool, *, batch):
        pytest.fail("must not sweep")

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(sweep.run_sweeper(object(), settings, stop, asyncio.Event()), 2)

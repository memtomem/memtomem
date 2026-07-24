"""Shielded settlement variants (#1803, #1806, #1936).

Three helpers differ only in what they *raise* versus *hand back*:

===========================  ==================  ====================
variant                      worker failure      cancellation
===========================  ==================  ====================
``settle_shielded``          raises              raises
``settle_shielded_result``   logs, returns None  returns
``settle_shielded_value``    raises              returns (with value)
===========================  ==================  ====================

The distinction is load-bearing rather than cosmetic: the lifecycle
barrier (#1936) fails closed, so an acquisition failure reaching it as
``None`` — the ``_result`` variant's contract — would let server startup
continue past an in-flight uninstall and reopen the TOCTOU the barrier
exists to close.
"""

from __future__ import annotations

import asyncio

import pytest

from memtomem._settlement import (
    settle_shielded,
    settle_shielded_result,
    settle_shielded_value,
)


async def _ok(value: object) -> object:
    return value


async def _boom(exc: BaseException) -> object:
    raise exc


class TestFailurePolarity:
    """The axis that separates the three variants."""

    @pytest.mark.asyncio
    async def test_value_variant_raises_the_original_failure(self) -> None:
        """Fail-closed callers need the actionable error itself — a
        ``PermissionError`` naming the unusable path, not ``None``."""
        boom = PermissionError(13, "runtime dir is not writable")
        future = asyncio.ensure_future(_boom(boom))
        with pytest.raises(PermissionError) as excinfo:
            await settle_shielded_value(future, what="probe")
        assert excinfo.value is boom

    @pytest.mark.asyncio
    async def test_result_variant_swallows_the_failure(self) -> None:
        """Contrast pin: this is why the barrier cannot use it."""
        future = asyncio.ensure_future(_boom(RuntimeError("nope")))
        result, cancelled = await settle_shielded_result(future, what="probe")
        assert result is None
        assert cancelled is None

    @pytest.mark.asyncio
    async def test_value_variant_returns_the_result_on_success(self) -> None:
        future = asyncio.ensure_future(_ok("handle"))
        result, cancelled = await settle_shielded_value(future, what="probe")
        assert result == "handle"
        assert cancelled is None


class TestCancellationPrecedence:
    @pytest.mark.asyncio
    async def test_success_plus_cancellation_hands_back_both(self) -> None:
        """The #1936 case: a handle acquired just as cancellation lands
        must reach the caller so it can be stored (and later released)
        *before* the caller re-raises the cancellation itself."""

        async def slow() -> str:
            await asyncio.sleep(0.2)
            return "handle"

        future = asyncio.ensure_future(slow())

        async def settle() -> tuple[object, asyncio.CancelledError | None]:
            return await settle_shielded_value(future, what="probe")

        task = asyncio.create_task(settle())
        await asyncio.sleep(0.05)
        task.cancel()
        result, cancelled = await task
        assert result == "handle", "the acquired value was dropped on the floor"
        assert isinstance(cancelled, asyncio.CancelledError)

    @pytest.mark.asyncio
    async def test_cancellation_wins_over_a_failure(self) -> None:
        """Shutdown paths rely on observing ``CancelledError``; a surprise
        teardown exception there would masquerade as a crash."""

        async def slow_boom() -> object:
            await asyncio.sleep(0.2)
            raise RuntimeError("worker failed")

        future = asyncio.ensure_future(slow_boom())

        async def settle() -> tuple[object, asyncio.CancelledError | None]:
            return await settle_shielded_value(future, what="probe")

        task = asyncio.create_task(settle())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_repeated_cancellation_preserves_the_first_instance(self) -> None:
        """A ``task.cancel(msg)`` message must survive settlement rather
        than being overwritten by a later bare cancel."""

        async def slow() -> str:
            await asyncio.sleep(0.3)
            return "handle"

        future = asyncio.ensure_future(slow())

        async def settle() -> tuple[object, asyncio.CancelledError | None]:
            return await settle_shielded_value(future, what="probe")

        task = asyncio.create_task(settle())
        await asyncio.sleep(0.05)
        task.cancel("first")
        await asyncio.sleep(0.05)
        task.cancel("second")
        _, cancelled = await task
        assert cancelled is not None
        assert cancelled.args == ("first",)


class TestSharedShieldContract:
    @pytest.mark.asyncio
    async def test_worker_is_never_cancelled_by_the_awaiter(self) -> None:
        """The shield is the whole point: a worker that cannot be
        interrupted must finish even when its awaiter is cancelled."""
        finished: list[str] = []

        async def slow() -> str:
            await asyncio.sleep(0.2)
            finished.append("done")
            return "handle"

        future = asyncio.ensure_future(slow())

        async def settle() -> tuple[object, asyncio.CancelledError | None]:
            return await settle_shielded_value(future, what="probe")

        task = asyncio.create_task(settle())
        await asyncio.sleep(0.05)
        task.cancel()
        await task
        assert finished == ["done"]

    @pytest.mark.asyncio
    async def test_strict_variant_still_raises_both(self) -> None:
        """Guard against the three variants drifting into each other."""
        future = asyncio.ensure_future(_boom(RuntimeError("nope")))
        with pytest.raises(RuntimeError):
            await settle_shielded(future, what="probe")

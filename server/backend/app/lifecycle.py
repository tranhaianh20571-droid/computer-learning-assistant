"""Compose application lifespans and unwind partially started workers."""
from contextlib import AsyncExitStack, asynccontextmanager


def worker_lifespan(workers, *, parent=None):
    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            if parent is not None:
                await stack.enter_async_context(parent(app))
            for worker in workers:
                stack.callback(worker.close)
                worker.start()
            yield
    return lifespan

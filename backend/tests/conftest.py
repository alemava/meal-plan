import pytest_asyncio

from app.core import db


@pytest_asyncio.fixture
async def db_pool():
    """Function-scoped deliberately, not session-scoped — asyncpg's pool is
    bound to the event loop it was created in, and pytest-asyncio's
    session-scoped fixtures don't reliably share a loop with test functions
    across versions (hit live: 'Future attached to a different loop').
    Function scope sidesteps the whole class of bug at the cost of a
    reconnect per test — fine at this project's test-suite size (one test
    today). There is no separate test DB/project at this project's scale —
    dev (jtgyttbobxtxgtnmuyas) IS the test environment, same as every other
    verification this session has run directly against it."""
    await db.connect()
    yield
    await db.disconnect()

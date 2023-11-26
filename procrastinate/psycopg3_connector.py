import asyncio
import functools
import logging
import re
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional

import psycopg
import psycopg.errors
import psycopg.sql
import psycopg.types.json
import psycopg_pool
from psycopg.rows import DictRow, dict_row

from procrastinate import connector, exceptions, sql

logger = logging.getLogger(__name__)

LISTEN_TIMEOUT = 30.0

CoroutineFunction = Callable[..., Coroutine]


def wrap_exceptions(coro: CoroutineFunction) -> CoroutineFunction:
    """
    Wrap psycopg3 errors as connector exceptions.

    This decorator is expected to be used on coroutine functions only.
    """

    @functools.wraps(coro)
    async def wrapped(*args, **kwargs):
        try:
            return await coro(*args, **kwargs)
        except psycopg.errors.UniqueViolation as exc:
            raise exceptions.UniqueViolation(constraint_name=exc.diag.constraint_name)
        except psycopg.Error as exc:
            raise exceptions.ConnectorException from exc

    # Attaching a custom attribute to ease testability and make the
    # decorator more introspectable
    wrapped._exceptions_wrapped = True  # type: ignore
    return wrapped


def wrap_query_exceptions(coro: CoroutineFunction) -> CoroutineFunction:
    """
    Detect "admin shutdown" errors and retry a number of times.

    This is to handle the case where the database connection (obtained from the pool)
    was actually closed by the server. In this case, pyscopg3 raises an AdminShutdown
    exception when the connection is used for issuing a query. What we do is retry when
    an AdminShutdown is raised, and until the maximum number of retries is reached.

    The number of retries is set to the pool maximum size plus one, to handle the case
    where the connections we have in the pool were all closed on the server side.
    """

    @functools.wraps(coro)
    async def wrapped(*args, **kwargs):
        final_exc = None
        try:
            max_tries = args[0]._pool.max_size + 1
        except Exception:
            max_tries = 1
        for _ in range(max_tries):
            try:
                return await coro(*args, **kwargs)
            except psycopg.errors.OperationalError as exc:
                if "server closed the connection unexpectedly" in str(exc):
                    final_exc = exc
                    continue
                raise exc
        raise exceptions.ConnectorException(
            f"Could not get a valid connection after {max_tries} tries"
        ) from final_exc

    return wrapped


PERCENT_PATTERN = re.compile(r"%(?![\(s])")


class Psycopg3Connector(connector.BaseAsyncConnector):
    def __init__(
        self,
        *,
        json_dumps: Optional[Callable] = None,
        json_loads: Optional[Callable] = None,
        **kwargs: Any,
    ):
        """
        Asynchronous connector based on a ``psycopg_pool.AsyncConnectionPool``.

        The pool connection parameters can be provided here. Alternatively, an already
        existing ``psycopg_pool.AsyncConnectionPool`` can be provided in the
        ``App.open_async``, via the ``pool`` parameter.

        All other arguments than ``json_dumps`` and ``json_loads`` are passed to
        :py:func:`AsyncConnectionPool` (see psycopg3 documentation__), with default
        values that may differ from those of ``psycopg3`` (see a partial list of
        parameters below).

        .. _psycopg3 doc: https://www.psycopg.org/psycopg3/docs/basic/adapt.html#json-adaptation
        .. __: https://www.psycopg.org/psycopg3/docs/api/pool.html
               #psycopg_pool.AsyncConnectionPool

        Parameters
        ----------
        json_dumps :
            The JSON dumps function to use for serializing job arguments. Defaults to
            the function used by psycopg3. See the `psycopg3 doc`_.
        json_loads :
            The JSON loads function to use for deserializing job arguments. Defaults
            to the function used by psycopg3. See the `psycopg3 doc`_. Unused if the
            pool is externally created and set into the connector through the
            ``App.open_async`` method.
        min_size : int
            Passed to psycopg3, default set to 1 (same as aiopg).
        max_size : int
            Passed to psycopg3, default set to 10 (same as aiopg).
        conninfo : ``Optional[str]``
            Passed to psycopg3. Default is "" instead of None, which means if no
            argument is passed, it will connect to localhost:5432 instead of a
            Unix-domain local socket file.
        """
        self.json_dumps = json_dumps
        self.json_loads = json_loads
        self._pool: Optional[psycopg_pool.AsyncConnectionPool] = None
        self._pool_args = self._adapt_pool_args(kwargs, json_loads)
        self._pool_externally_set = False

    @staticmethod
    def _adapt_pool_args(
        pool_args: Dict[str, Any], json_loads: Optional[Callable]
    ) -> Dict[str, Any]:
        """
        Adapt the pool args for ``psycopg3``, using sensible defaults for Procrastinate.
        """
        base_configure = pool_args.pop("configure", None)

        @wrap_exceptions
        async def configure(connection: psycopg.AsyncConnection[DictRow]):
            if base_configure:
                await base_configure(connection)
            if json_loads:
                psycopg.types.json.set_json_loads(json_loads, connection)

        return {
            "conninfo": "",
            "min_size": 1,
            "max_size": 10,
            "kwargs": {
                "row_factory": dict_row,
            },
            "configure": configure,
            "open": False,
            **pool_args,
        }

    async def open_async(
        self, pool: Optional[psycopg_pool.AsyncConnectionPool] = None
    ) -> None:
        """
        Instantiate the pool.

        pool :
            Optional pool. Procrastinate can use an existing pool. Connection parameters
            passed in the constructor will be ignored.
        """
        if self._pool:
            return

        if pool:
            self._pool_externally_set = True
            self._pool = pool
        else:
            self._pool = await self._create_pool(self._pool_args)

        # ensure pool is open
        await self._pool.open()  # type: ignore

    @staticmethod
    @wrap_exceptions
    async def _create_pool(
        pool_args: Dict[str, Any]
    ) -> psycopg_pool.AsyncConnectionPool:
        return psycopg_pool.AsyncConnectionPool(**pool_args)

    @wrap_exceptions
    async def close_async(self) -> None:
        """
        Close the pool and awaits all connections to be released.
        """
        if not self._pool or self._pool_externally_set:
            return

        await self._pool.close()
        self._pool = None

    @property
    def pool(
        self,
    ) -> psycopg_pool.AsyncConnectionPool[psycopg.AsyncConnection[DictRow]]:
        if self._pool is None:  # Set by open
            raise exceptions.AppNotOpen
        return self._pool

    def _wrap_json(self, arguments: Dict[str, Any]):
        return {
            key: psycopg.types.json.Jsonb(value, dumps=self.json_dumps)
            if isinstance(value, dict)
            else value
            for key, value in arguments.items()
        }

    @wrap_exceptions
    @wrap_query_exceptions
    async def execute_query_async(self, query: str, **arguments: Any) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, self._wrap_json(arguments))

    @wrap_exceptions
    @wrap_query_exceptions
    async def execute_query_one_async(
        self, query: str, **arguments: Any
    ) -> Optional[DictRow]:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, self._wrap_json(arguments))
                return await cursor.fetchone()

    @wrap_exceptions
    @wrap_query_exceptions
    async def execute_query_all_async(
        self, query: str, **arguments: Any
    ) -> List[DictRow]:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, self._wrap_json(arguments))
                return await cursor.fetchall()

    @wrap_exceptions
    async def listen_notify(
        self, event: asyncio.Event, channels: Iterable[str]
    ) -> None:
        # We need to acquire a dedicated connection, and use the listen
        # query
        if self.pool.max_size == 1:
            logger.warning(
                "Listen/Notify capabilities disabled because maximum pool size"
                "is set to 1",
                extra={"action": "listen_notify_disabled"},
            )
            return

        query_template = psycopg.sql.SQL(sql.queries["listen_queue"])

        while True:
            async with self.pool.connection() as connection:
                # autocommit is required for async connection notifies
                await connection.set_autocommit(True)

                for channel_name in channels:
                    query = query_template.format(
                        channel_name=psycopg.sql.Identifier(channel_name)
                    )
                    await connection.execute(query)

                event.set()

                await self._loop_notify(event=event, connection=connection)

    @wrap_exceptions
    async def _loop_notify(
        self,
        event: asyncio.Event,
        connection: psycopg.AsyncConnection,
    ) -> None:
        while True:
            if connection.closed:
                return
            try:
                notifies = connection.notifies()
                async for _ in notifies:
                    event.set()
            except psycopg.OperationalError:
                continue

    def __del__(self):
        pass
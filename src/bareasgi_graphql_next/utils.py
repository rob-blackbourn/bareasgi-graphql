"""
Utilities
"""

import asyncio
from asyncio import Event
from typing import AsyncIterator, Optional, Set, Any

import bareutils.header as header
import graphql

from baretypes import Scope
from graphql import OperationType
from graphql.subscription.map_async_iterator import MapAsyncIterator


async def cancellable_aiter(
        async_iterator: MapAsyncIterator,
        cancellation_event: Event,
        *,
        cancel_pending: bool = True,
        timeout: Optional[float] = None
) -> AsyncIterator:
    """Create a cancellable async iterator.

    :param async_iterator: The async iterator to wrap
    :param cancellation_event: The asyncio Event to controll cancellation.
    :param cancel_pending: If True cancel pending tasks, otherwise wait them.
    :return: The wrapped async iterator
    """
    result_iter = async_iterator.__aiter__()
    cancellation_task = asyncio.create_task(cancellation_event.wait())
    pending: Set[asyncio.Future[Any]] = {
        cancellation_task,
        asyncio.create_task(result_iter.__anext__())
    }

    if timeout is None:
        sleep_task = None
    else:
        sleep_task = asyncio.create_task(asyncio.sleep(timeout))
        pending.add(sleep_task)

    while not cancellation_event.is_set():
        try:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for pending_task in pending:
                pending_task.cancel()
            raise

        for done_task in done:
            if done_task == cancellation_task:
                for pending_task in pending:
                    if cancel_pending:
                        pending_task.cancel()
                    else:
                        await pending_task
                        yield pending_task.result()
                break
            elif done_task == sleep_task:
                yield None
            else:
                yield done_task.result()
                pending.add(asyncio.create_task(result_iter.__anext__()))
        else:
            if timeout is not None:
                if sleep_task in pending:
                    sleep_task.cancel()
                    pending.discard(sleep_task)
                sleep_task = asyncio.create_task(asyncio.sleep(timeout))
                pending.add(sleep_task)



def _is_http_2(scope: Scope) -> bool:
    return scope['http_version'] in ('2', '2.0')


def get_host(scope: Scope) -> bytes:
    """Get the host from the scope"""
    if _is_http_2(scope):
        return header.find(b':authority', scope['headers'])
    else:
        return header.find(b'host', scope['headers'])


def _is_subscription(definition: graphql.DefinitionNode) -> bool:
    return isinstance(
        definition,
        graphql.OperationDefinitionNode
    ) and definition.operation is OperationType.SUBSCRIPTION


def has_subscription(document: graphql.DocumentNode) -> bool:
    """Find if a document has a subscription

    :param document: The GraphQL query document
    :type document: graphql.DocumentNode
    :return: True if the document contains a subscription
    :rtype: bool
    """
    return any(_is_subscription(definition) for definition in document.definitions)

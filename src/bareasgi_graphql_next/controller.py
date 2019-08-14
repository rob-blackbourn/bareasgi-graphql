"""
GraphQL controller
"""

import asyncio
import io
import json
import logging

from cgi import parse_multipart
from datetime import datetime
from typing import List, Dict, Any, Optional, Mapping
from urllib.parse import parse_qs, urlencode

import graphql
import bareutils.header as header

from graphql import GraphQLSchema
from bareasgi import Application
from bareutils import text_reader, text_writer, response_code
from baretypes import (
    Header,
    HttpResponse,
    Scope,
    Info,
    RouteMatches,
    Content,
    WebSocket,
    HttpMiddlewareCallback
)

from .template import make_template
from .websocket_handler import GraphQLWebSocketHandler
from .utils import cancellable_aiter, has_subscription, get_host, wrap_middleware

logger = logging.getLogger(__name__)

class GraphQLController:
    """GraphQL Controller"""

    def __init__(
            self,
            schema: GraphQLSchema,
            path_prefix: str = '',
            middleware=None,
            subscription_expiry: float = 60,
            ping_interval: float = 10
    ) -> None:
        self.schema = schema
        self.path_prefix = path_prefix
        self.middleware = middleware
        self.subscription_expiry = subscription_expiry
        self.ping_interval = ping_interval
        self.ws_subscription_handler = GraphQLWebSocketHandler(schema)
        self.cancellation_event = asyncio.Event()

    def shutdown(self) -> None:
        """Shutdown the service"""
        self.cancellation_event.set()

    # pylint: disable=unused-argument
    async def view_graphiql(
            self,
            scope: Scope,
            info: Info,
            matches: RouteMatches,
            content: Content
    ) -> HttpResponse:
        """Render the Graphiql view"""

        host = get_host(scope).decode('ascii')
        body = make_template(
            host,
            self.path_prefix + '/graphql',
            self.path_prefix + '/subscriptions'
        )
        headers = [
            (b'content-type', b'text/html'),
            (b'content-length', str(len(body)).encode())
        ]
        return response_code.OK, headers, text_writer(body)

    async def handle_subscription(
            self,
            scope: Scope,
            info: Info,
            matches: RouteMatches,
            web_socket: WebSocket
    ) -> None:
        """Handle a websocket subscription"""
        await self.ws_subscription_handler(scope, info, matches, web_socket)

    @classmethod
    async def _get_query_document(
            cls,
            headers: List[Header],
            content: Content
    ) -> Mapping[str, Any]:
        content_type, parameters = header.content_type(headers)

        if content_type == b'application/graphql':
            return {'query': await text_reader(content)}
        elif content_type in (b'application/json', b'text/plain'):
            return json.loads(await text_reader(content))
        elif content_type == b'application/x-www-form-urlencoded':
            body = parse_qs(await text_reader(content))
            return {name: value[0] for name, value in body.items()}
        elif content_type == b'multipart/form-data':
            return {
                name: value[0]
                for name, value in parse_multipart(
                    io.StringIO(await text_reader(content)),
                    {key.decode('utf-8'): val for key, val in parameters.items()}
                ).items()
            }
        else:
            raise RuntimeError('Content type not supported')

    # pylint: disable=unused-argument
    async def handle_graphql(
            self,
            scope: Scope,
            info: Info,
            matches: RouteMatches,
            content: Content
    ) -> HttpResponse:
        """A request handler for graphql queries"""

        try:
            body = await self._get_query_document(scope['headers'], content)

            query: str = body['query']
            variables: Optional[Dict[str, Any]] = body.get('variables')
            operation_name: Optional[str] = body.get('operationName')

            query_document = graphql.parse(query)

            if has_subscription(query_document):
                # Handle a subscription by returning 201 (Created) with
                # the url location of the subscription.
                scheme = scope['scheme']
                host = get_host(scope).decode('utf-8')
                path = self.path_prefix + '/sse-subscription'
                query_string = urlencode(
                    {
                        name.encode('utf-8'): json.dumps(value).encode('utf-8')
                        for name, value in body.items()
                    }
                )
                location = f'{scheme}://{host}{path}?{query_string}'
                headers = [
                    (b'access-control-expose-headers', b'location'),
                    (b'location', location.encode('ascii'))
                ]
                return response_code.CREATED, headers
            else:
                # Handle a query
                result = await graphql.graphql(
                    schema=self.schema,
                    source=graphql.Source(query),  # source=query,
                    variable_values=variables,
                    operation_name=operation_name,
                    context_value=info,
                    middleware=self.middleware
                )

                response: Dict[str, Any] = {'data': result.data}
                if result.errors:
                    response['errors'] = [error.formatted for error in result.errors]

                text = json.dumps(response)
                headers = [
                    (b'content-type', b'application/json'),
                    (b'content-length', str(len(text)).encode())
                ]

                return 200, headers, text_writer(text)

        # pylint: disable=bare-except
        except:
            text = 'Internal server error'
            headers = [
                (b'content-type', b'text/plain'),
                (b'content-length', str(len(text)).encode())
            ]
            return response_code.INTERNAL_SERVER_ERROR, headers, text_writer(text)

    async def handle_sse(
            self,
            scope: Scope,
            info: Info,
            matches: RouteMatches,
            content: Content
    ) -> HttpResponse:
        """Handle a server sent event style direct subscription"""

        body = {
            name.decode('utf-8'): json.loads(value[0].decode('utf-8'))
            for name, value in parse_qs(scope['query_string']).items()
        }

        result = await graphql.subscribe(
            schema=self.schema,
            document=graphql.parse(body['query']),
            variable_values=body.get('variables'),
            operation_name=body.get('operationName'),
            context_value=info
        )

        logger.debug('SSE received subscription request: http_version=%s', scope['http_version'])

        # Make an async iterator for the subscription results.
        async def send_events():
            logger.debug('Started SSE subscription')

            try:
                async for val in cancellable_aiter(
                        result,
                        self.cancellation_event,
                        timeout=self.ping_interval
                ):
                    if val is None:
                        message = f'event: ping\ndata: {datetime.utcnow()}\n\n'.encode('utf-8')
                    else:
                        message = f'event: message\ndata: {json.dumps(val)}\n\n'.encode('utf-8')

                    yield message
                    # Give the ASGI server a nudge.
                    yield ':\n\n'.encode('utf-8')
            except asyncio.CancelledError:
                logger.debug("Cancelled SSE subscription")

            logger.debug('Stopped SSE subscription')

        headers = [
            (b'cache-control', b'no-cache'),
            (b'content-type', b'text/event-stream'),
            (b'connection', b'keep-alive')
        ]

        return response_code.OK, headers, send_events()



def add_graphql_next(
        app: Application,
        schema: GraphQLSchema,
        path_prefix: str = '',
        rest_middleware: Optional[HttpMiddlewareCallback] = None,
        view_middleware: Optional[HttpMiddlewareCallback] = None,
        graphql_middleware=None,
        subscription_expiry: float = 60,
        ping_interval: float = 10
) -> GraphQLController:
    """Add graphql support to an bareASGI application.

    :param app: The bareASGI application.
    :param schema: The GraphQL schema to use.
    :param path_prefix: An optional path prefix from which to provide endpoints.
    :param rest_middleware: Middleware for the rest end points.
    :param view_middleware: Middleware from the GraphiQL end point.
    :param graphql_middleware: Middleware for graphql-core-next.
    :param subscription_expiry: The time to wait before abandoning an unused subscription.
    :return: Returns the constructed controller.
    """
    controller = GraphQLController(
        schema,
        path_prefix,
        graphql_middleware,
        subscription_expiry,
        ping_interval
    )

    # Add the REST route
    app.http_router.add(
        {'GET'},
        path_prefix + '/graphql',
        wrap_middleware(rest_middleware, controller.handle_graphql)
    )
    app.http_router.add(
        {'POST', 'OPTION'},
        path_prefix + '/graphql',
        wrap_middleware(rest_middleware, controller.handle_graphql)
    )
    app.http_router.add(
        {'GET'},
        path_prefix + '/sse-subscription',
        wrap_middleware(rest_middleware, controller.handle_sse)
    )

    # Add the subscription route
    app.ws_router.add(
        path_prefix + '/subscriptions',
        controller.handle_subscription
    )

    # Add Graphiql
    app.http_router.add(
        {'GET'},
        path_prefix + '/graphiql',
        wrap_middleware(view_middleware, controller.view_graphiql)
    )

    return controller

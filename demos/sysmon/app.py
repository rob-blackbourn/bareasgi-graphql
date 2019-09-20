"""
The ASGI Application
"""

import asyncio
import logging
import string


from bareasgi import Application
from bareasgi_cors import CORSMiddleware
from bareasgi_graphql_next import add_graphql_next
from baretypes import Scope, Info, RouteMatches, Content, HttpResponse
from bareutils import text_writer
from bareasgi_graphql_next.controller import get_host

from .system_monitor import SystemMonitor
from .schema import schema
from .html_template import make_html

logger = logging.getLogger(__name__)

# pylint: disable=unused-argument
async def start_service(scope: Scope, info: Info, request) -> None:
    """Start the service"""
    system_monitor = SystemMonitor(30)

    info['system_monitor'] = system_monitor
    info['system_monitor_task'] = asyncio.create_task(system_monitor.startup())


# pylint: disable=unused-argument
async def stop_service(scope: Scope, info: Info, request) -> None:
    """Stop the service"""
    system_monitor: SystemMonitor = info['system_monitor']
    system_monitor_task: asyncio.Task = info['system_monitor_task']
    system_monitor.shutdown()
    await system_monitor_task


# pylint: disable=unused-argument
async def graphql_handler(
        scope: Scope,
        info: Info,
        matches: RouteMatches,
        content: Content
) -> HttpResponse:
    """Handle a graphql request"""
    host = get_host(scope).decode()
    sse_url = f"{scope['scheme']}://{host}/test/graphql"
    html = make_html(sse_url)
    return 200, [(b'content-type', b'text/html')], text_writer(html)


def make_application() -> Application:
    """Make the application"""
    cors_middleware = CORSMiddleware()
    info: dict = {}
    app = Application(
        info=info,
        startup_handlers=[start_service],
        shutdown_handlers=[stop_service],
        middlewares=[cors_middleware]
    )
    add_graphql_next(app, schema, '/test')

    app.http_router.add({'GET'}, '/test/graphql2', graphql_handler)

    return app

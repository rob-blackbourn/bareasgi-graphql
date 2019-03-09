from bareasgi import Info, WebSocket
import graphql
import json
import logging
from typing import Any, Mapping, MutableMapping, Optional, AsyncIterator, Tuple, Union

logger = logging.getLogger(__name__)

WS_INTERNAL_ERROR = 1011

GQL_CONNECTION_INIT = "connection_init"  # Client -> Server
GQL_CONNECTION_ACK = "connection_ack"  # Server -> Client
GQL_CONNECTION_ERROR = "connection_error"  # Server -> Client
GQL_CONNECTION_KEEP_ALIVE = "ka"  # Server -> Client
GQL_CONNECTION_TERMINATE = "connection_terminate"  # Client -> Server
GQL_START = "start"  # Client -> Server
GQL_DATA = "data"  # Server -> Client
GQL_ERROR = "error"  # Server -> Client
GQL_COMPLETE = "complete"  # Server -> Client
GQL_STOP = "stop"  # Client -> Server


class ProtocolError(Exception):
    pass


class GraphQLWebSocketHandlerInstance:

    def __init__(self, schema: graphql.GraphQLSchema, web_socket: WebSocket, info: Info) -> None:
        self.schema = schema
        self.web_socket = web_socket
        self.info = info
        self._subscriptions: MutableMapping[str, AsyncIterator[graphql.ExecutionResult]] = {}

    async def start(self):
        await self._on_open()

        _type = GQL_CONNECTION_KEEP_ALIVE
        try:
            while _type not in (GQL_CONNECTION_ERROR, GQL_CONNECTION_TERMINATE):
                type_, id_, payload = await self._read_message()
                await self._on_message(type_, id_, payload)

        except ProtocolError as error:
            logger.error(f'Failed to process message: {error}')
        except Exception as error:
            logger.error(f'Internal error: {error}')

        await self._on_close()

    async def _on_open(self):
        await self.web_socket.accept()

    async def _on_close(self) -> None:
        await self._unsubscribe_all()
        await self.web_socket.close()

    async def _read_message(self) -> Tuple[str, Optional[str], Optional[dict]]:
        text = await self.web_socket.receive()
        if not isinstance(text, str):
            raise ProtocolError('Expected the message to be a string.')

        message = json.loads(text)
        if not isinstance(message, dict):
            raise ProtocolError('Expected the message to be an object.')

        type_ = message['type']
        if not isinstance(type_, str):
            raise ProtocolError("Expected field 'type' to be a string")

        id_ = message.get('id')
        if not (id_ is None or isinstance(id_, str)):
            raise ProtocolError("Expected field 'id' to be a string?")

        payload = message.get('payload')
        if not (payload is None or isinstance(payload, dict)):
            raise ProtocolError("Expected field 'payload' to be an object?")

        return type_, id_, payload

    async def _on_message(self, type_: str, id_: Optional[str], payload: Optional[dict]):

        if type_ == GQL_CONNECTION_INIT:
            await self._on_connection_init(id_, payload)
        elif type_ == GQL_CONNECTION_TERMINATE:
            await self._on_connection_terminate()
        elif type_ == GQL_START:
            await self._on_start(id_, payload)
        elif type_ == GQL_STOP:
            await self._on_stop(id_)
        elif type_ == GQL_CONNECTION_KEEP_ALIVE:
            pass
        else:
            raise ProtocolError(f"Received unknown message type '{type_}'.")

    async def _on_connection_init(self, id_: Optional[str], connection_params: Optional[Any]):
        try:
            await self.web_socket.send(self.to_message('connection_ack', id_))
        except Exception as error:
            await self._send_error(GQL_CONNECTION_ERROR, id_, error)
            await self.web_socket.close(WS_INTERNAL_ERROR)
            raise

    async def _on_connection_terminate(self):
        await self.web_socket.close(WS_INTERNAL_ERROR)

    async def _on_start(self, id_: Optional[str], payload: Optional[Union[list, dict]]):
        try:
            # An id is required for a start operation.
            if id_ is None:
                raise ProtocolError("required 'id' field must be an int.")

            if id_ in self._subscriptions:
                await self._unsubscribe(id_)
                del self._subscriptions[id_]

            query, variable_values, operation_name = self.parse_start_payload(payload)

            document = graphql.parse(query)
            if any(definition.operation is graphql.OperationType.SUBSCRIPTION for definition in document.definitions):
                result = await graphql.subscribe(
                    self.schema,
                    document=document,
                    context_value=self.info,
                    variable_values=variable_values,
                    operation_name=operation_name
                )
            else:
                result = await graphql.graphql(
                    self.schema,
                    source=graphql.Source(query),
                    context_value=self.info,
                    variable_values=variable_values,
                    operation_name=operation_name
                )

            if not isinstance(result, AsyncIterator):
                await self._send_execution_result(id_, result)
                return True

            self._subscriptions[id_] = result
            try:
                async for val in result:
                    await self._send_execution_result(id_, val)
            finally:
                del self._subscriptions[id_]
                await self.web_socket.send(self.to_message(GQL_COMPLETE, id_))

        except Exception as error:
            await self._send_error(GQL_ERROR, id_, error)

    async def _on_stop(self, id_: str):
        await self._unsubscribe(id_)

    async def _unsubscribe(self, id_: str) -> None:
        await self._subscriptions[id_].aclose()

    async def _unsubscribe_all(self):
        for id_ in self._subscriptions.keys():
            await self._unsubscribe(id_)

    async def _send_error(self, type_: str, id_, error: Exception) -> None:
        await self.web_socket.send(self.to_message(type_, id_, {'message': str(error)}))

    async def _send_execution_result(self, id_: str, execution_result: graphql.ExecutionResult) -> None:
        result = dict()

        if execution_result.data:
            result["data"] = execution_result.data

        if execution_result.errors:
            result["errors"] = [graphql.format_error(error) for error in execution_result.errors]

        await self.web_socket.send(self.to_message(GQL_DATA, id_, result))

    @classmethod
    def to_message(cls, type_: str, id_: Optional[str] = None, payload: Optional[Any] = None) -> str:
        message = {'type': type_}
        if id_ is not None:
            message['id'] = id_
        if payload is not None:
            message['payload'] = payload
        return json.dumps(message)

    @classmethod
    def parse_start_payload(
            cls,
            payload: Optional[Union[dict, list]]
    ) -> Tuple[str, Optional[Mapping[str, Any]], Optional[str]]:

        if not isinstance(payload, dict):
            raise ProtocolError("required 'payload' field must be an object.")

        query = payload.get('query')
        if not isinstance(query, str):
            raise ProtocolError("required 'query' field must be string in 'payload'.")

        variable_values = payload.get('variables')
        if not (variable_values is None or isinstance(variable_values, dict)):
            raise ProtocolError("optional 'variables' field must be object? in 'payload'.")

        operation_name = payload.get('operationName')
        if not (operation_name is None or isinstance(operation_name, str)):
            raise ProtocolError("optional 'operationName' field must be str? in 'payload'.")

        return query, variable_values, operation_name
import asyncio
import itertools
import json
from uuid import uuid4
from typing import Dict, List, Optional

import pika
from pika import ConnectionParameters
from pika.channel import Channel
from pika.spec import Basic
from pika.spec import BasicProperties

from pika.adapters.select_connection import SelectConnection
from core.transport.base import AbstractTransportGateway
from core.transport.z_dev_config import AGENT_NAME, TRANSPORT_TIMEOUT_SECS, RABBIT_MQ, ANNOTATORS, SKILL_SELECTORS
from core.transport.z_dev_config import SKILLS, RESPONSE_SELECTORS, POSTPROCESSORS


AGENT_IN_EXCHANGE_NAME = f'e_{AGENT_NAME}_in'
AGENT_IN_QUEUE_NAME = f'q_agent_{AGENT_NAME}_in'
AGENT_IN_ROUTING_KEY = '{}.{}'

AGENT_OUT_EXCHANGE_NAME = f'e_{AGENT_NAME}_out'

SERVICE_IN_QUEUE_NAME = 'q_service_{}_in'
SERVICE_IN_ROUTER_KEY_ANY = '{}.anyinstance'
SERVICE_IN_ROUTER_KEY_INSTANCE = '{}.instance.{}'


# TODO: add load balancing for stateful skills
class RabbitMQTransportGatewey(AbstractTransportGateway):
    _service_names: List[str]
    _connection_parameters: ConnectionParameters

    _producer_connection: SelectConnection
    _producer_channel: Channel
    _consumer_connection: SelectConnection
    _consumer_channel: Channel

    _service_responded_events: Dict[str, asyncio.Event]
    _service_responses: Dict[str, dict]

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._service_names = [service['name'] for service in itertools.chain[ANNOTATORS, SKILL_SELECTORS, SKILLS,
                                                                              RESPONSE_SELECTORS, POSTPROCESSORS]]

        self._connection_parameters = ConnectionParameters(host=RABBIT_MQ['host'], port=RABBIT_MQ['PORT'])

        self._service_responded_events = {}
        self._service_responses = {}

    def _connect_producer(self) -> None:
        self._producer_connection = SelectConnection(parameters=self._connection_parameters,
                                                     on_open_callback=self._on_connect_producer)

    def _on_connect_producer(self, connection: SelectConnection) -> None:
        self._producer_channel = connection.channel(on_open_callback=self._on_open_producer_channel)

    def _on_open_producer_channel(self, channel: Channel) -> None:
        channel.exchange_declare(exchange=AGENT_OUT_EXCHANGE_NAME, exchange_type='topic')

        for service_name in self._service_names:
            channel.queue_declare(SERVICE_IN_QUEUE_NAME.format(service_name), durable=True)

    def _connect_consumer(self) -> None:
        self._consumer_connection = SelectConnection(parameters=self._connection_parameters,
                                                     on_open_callback=self._on_connect_consumer)

    def _on_connect_consumer(self, connection: SelectConnection) -> None:
        self._consumer_channel = connection.channel(on_open_callback=self._on_open_consumer_channel)

    def _on_open_consumer_channel(self, channel: Channel) -> None:
        channel.exchange_declare(exchange=AGENT_IN_EXCHANGE_NAME, exchange_type='topic')
        channel.queue_declare(AGENT_IN_QUEUE_NAME, durable=True)
        channel.queue_bind(exchange=AGENT_IN_EXCHANGE_NAME, queue=AGENT_IN_QUEUE_NAME, routing_key='#')
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=AGENT_IN_QUEUE_NAME, on_message_callback=self._on_message_callback)

    async def _on_message_callback(self, channel: Channel, method: Basic.Deliver,
                                   _properties: BasicProperties, body: bytes) -> None:
        processed_message: dict = json.loads(body, encoding='utf-8')
        message_uuid = processed_message['message_uuid']
        dialog_state = processed_message['dialog_state']
        message_event = self._service_responded_events.pop(message_uuid, None)

        if message_event and not message_event.is_set():
            self._service_responses[message_uuid] = dialog_state
            message_event.set()

        channel.basic_ack(delivery_tag=method.delivery_tag)

    async def process(self, service: str, dialog_state: dict) -> Optional[dict]:
        message_uuid = str(uuid4())

        message = {
            'message_uuid': message_uuid,
            'dialog_state': dialog_state
        }

        self._service_responded_events[message_uuid] = asyncio.Event()
        self._producer_channel.basic_publish(exchange=AGENT_OUT_EXCHANGE_NAME,
                                             routing_key=SERVICE_IN_ROUTER_KEY_ANY.format(service),
                                             body=json.dumps(message),
                                             properties=pika.BasicProperties(delivery_mode=2))

        try:
            await asyncio.wait_for(self._service_responded_events[message_uuid].wait(), TRANSPORT_TIMEOUT_SECS)
            updated_dialog_state = self._service_responses.pop(message_uuid, None)
        except asyncio.TimeoutError:
            updated_dialog_state = None
        finally:
            self._service_responded_events.pop(message_uuid, None)

        return updated_dialog_state

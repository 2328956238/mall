from aiokafka import AIOKafkaProducer
import json
from app.config import settings

producer: AIOKafkaProducer = None

async def start_kafka_producer():
    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode()
    )
    await producer.start()

async def stop_kafka_producer():
    if producer:
        await producer.stop()

async def publish_order_event(event_data: dict):
    await producer.send(settings.KAFKA_ORDER_TOPIC, value=event_data)
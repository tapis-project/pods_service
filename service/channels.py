from tapisservice.config import conf
from stores import get_site_rabbitmq_uri
from queues import BinaryTaskQueue
from tapisservice.tapisfastapi.utils import g
import pika
import pickle

def site():
    site_id = g.site_id or conf.get('site_id')
    return site_id

RABBIT_URI = get_site_rabbitmq_uri(site())

class CommandChannel(BinaryTaskQueue):
    """Work with commands on the command channel."""

    def __init__(self, name: str = "tacc"):
        self.uri = RABBIT_URI
        queues_list = ["tacc"]
        #queues_list = conf.get('spawner_host_queues')
        if name not in queues_list:
            raise Exception(f'Invalid Queue name: {name}')

        super().__init__(name=f'command_channel_{name}')

    def put_cmd(self, object_id, object_type, tenant_id, site_id, resolved_secrets=None):
        """Put a new command on the command channel. """
        msg = {'object_id': object_id,
               'object_type': object_type,
               'tenant_id': tenant_id,
               'site_id': site_id,
               'resolved_secrets': resolved_secrets or {}}

        self.put(msg)

class PikaCommandChannel:
    """Work with commands on the command channel using pika."""
    def __init__(self, name: str = "tacc"):
        self.queue_name = f'command_channel_{name}'
        self.uri = get_site_rabbitmq_uri(site())
        params = pika.URLParameters(self.uri)
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue=self.queue_name, durable=True)

    def put_cmd(self, object_id, object_type, tenant_id, site_id, resolved_secrets=None):
        msg = {'object_id': object_id,
               'object_type': object_type,
               'tenant_id': tenant_id,
               'site_id': site_id,
               'resolved_secrets': resolved_secrets or {}}
        self.channel.basic_publish(
            exchange='',
            routing_key=self.queue_name,
            body=pickle.dumps(msg),
            properties=pika.BasicProperties(delivery_mode=2)
        )

    def get_one(self):
        try:
            method_frame, header_frame, body = self.channel.basic_get(queue=self.queue_name, auto_ack=False)
            if method_frame:
                msg = pickle.loads(body)
                return msg, (lambda: self.channel.basic_ack(delivery_tag=method_frame.delivery_tag))
            else:
                return None, None
        except Exception as e:
            print(f"Error getting message from queue: {e}")
            return None, None

    def close(self):
        self.connection.close()



from tapisservice.config import conf
from stores import get_site_rabbitmq_uri
from queues import BinaryTaskQueue
from req_utils import g

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
            raise Exception('Invalid Queue name.')

        super().__init__(name=f'command_channel_{name}')

    def put_cmd(self, pod_name, tenant_id, site_id):
        """Put a new command on the command channel."""
        msg = {'pod_name': pod_name,
               'tenant_id': tenant_id,
               'site_id': site_id}

        self.put(msg)

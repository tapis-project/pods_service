import json
import os
import time

import rabbitpy
from concurrent.futures import ThreadPoolExecutor
from codes import ERROR, SPAWNER_SETUP, CREATING_CONTAINER, \
    REQUESTED, SHUTTING_DOWN, ON
from health import graceful_rm_pod
from models import Pod, Password
from channels import CommandChannel
from kubernetes_templates import start_generic_pod, start_neo4j_pod, start_postgres_pod
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
logger = get_logger(__name__)


class SpawnerException(BaseTapisError):
    """Error with spawner."""
    pass

class Spawner(object):
    def __init__(self):
        self.queue = os.environ.get('queue', 'tacc')
        self.cmd_ch = CommandChannel(name=self.queue)
        self.host_id = conf.spawner_host_id

    def run(self):
        executor = ThreadPoolExecutor(6) # 6 threads, meaning 6 spawning processes at once.
        while True:
            cmd, msg_obj = self.cmd_ch.get_one()
            # directly ack the messages from the command channel; problems generated from starting pods are
            # handled downstream; e.g., by setting the pod to an ERROR state; command messages should not be re-queued
            msg_obj.ack()
            try:
                executor.submit(self.process, cmd)
            except Exception as e:
                logger.error(f"Spawner got an exception trying to process cmd: {cmd}. "
                             f"Exception type: {type(e).__name__}. Exception: {e}")

    def process(self, cmd):
        """Main spawner method for processing a command from the CommandChannel."""
        logger.info(f"top of process; cmd: {cmd}")
        pod_id = cmd["pod_id"]
        tenant_id = cmd["tenant_id"]
        site_id = cmd["site_id"]

        # Get pod while in spawner. Expect REQUESTED. If status_requested = OFF then request was started while waiting
        # for command to startup in queue. In that case, we simply abort and wait for health to delete pod.
        try:
            pod = Pod.db_get_with_pk(pod_id, tenant=tenant_id, site=site_id)
        except Exception as e:
            msg = f"Exception in spawner trying to retrieve pod object from store. Aborting. Exception: {e}"
            logger.error(msg)
            return
        
        status = getattr(pod, 'status', '')
        if not status == REQUESTED:
            logger.debug(f"Spawner found pod NOT in REQUESTED status as expected. status: {status}. Returning and not processing command.")
            return

        status_requested = getattr(pod, 'status_requested', '')
        if not status_requested == ON:
            logger.debug(f"Spawner found pod not requesting ON as expected. status_requested: {status_requested}. Returning and not processing command.")
            return

        # Pod status was REQUESTED and status_requested was ON; moving on to SPAWNER_SETUP ----
        pod.status = SPAWNER_SETUP
        pod.db_update()
        logger.debug(f"spawner has updated pod status to SPAWNER_SETUP")

        try:
            if pod.pod_template.startswith("custom-"):
                custom_image = pod.pod_template.replace("custom-", "")
                start_generic_pod(pod=pod, custom_image=custom_image, revision=1)
            elif pod.pod_template == 'neo4j':
                start_neo4j_pod(pod=pod, revision=1)
            elif pod.pod_template == 'postgres':
                start_postgres_pod(pod=pod, revision=1)
            else:
                logger.critical(f"pod_template found no working functions. Running graceful_rm_pod.")
                graceful_rm_pod(pod)
                return
        except Exception as e:
            logger.critical(f"Got error when creating pod. Running graceful_rm_pod. e: {e}")
            graceful_rm_pod(pod)
            return

        # If we get to this point we can update pod status
        pod.status = CREATING_CONTAINER
        pod.db_update()
        logger.debug(f"spawner has updated pod status to CREATING_CONTAINER")

def main():
    # todo - find something more elegant
    # Ensure Mongo can connect.
    msg = "Spawner started. Connecting to rabbitmq..."
    logger.debug(msg)
    print(msg)
    # Start spawner
    idx = 0
    while idx < 6:
        try:
            sp = Spawner()
            logger.info("spawner made connection to rabbit, entering main loop")
            sp.run()
        except (rabbitpy.exceptions.ConnectionException, RuntimeError):
            # rabbit seems to take a few seconds to come up
            time.sleep(5)
            idx += 1
    logger.critical("spawner could not connect to rabbitMQ. Shutting down!")

if __name__ == '__main__':
    main()

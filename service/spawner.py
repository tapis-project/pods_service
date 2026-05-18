import json
import os
import time

import rabbitpy
from concurrent.futures import ThreadPoolExecutor
from codes import ERROR, SPAWNER_SETUP, CREATING, REQUESTED, DELETING, ON
from health import graceful_rm_pod, graceful_rm_volume
from models_pods import Pod, Password
from models_volumes import Volume
from channels import PikaCommandChannel
from kubernetes_templates import start_generic_pod #, start_neo4j_pod, start_postgres_pod
from kubernetes_utils import create_pvc
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
logger = get_logger(__name__)


class SpawnerException(BaseTapisError):
    """Error with spawner."""
    pass

class Spawner(object):
    def __init__(self):
        self.queue = os.environ.get('queue', 'tacc') # Which site is being worked on by this spawner.
        self.cmd_ch = PikaCommandChannel(name=self.queue)
        self.host_id = conf.spawner_host_id

    def run(self):
        executor = ThreadPoolExecutor(6) # 6 threads, meaning 6 spawning processes at once.
        while True:
            try:
                cmd, ack_fn = self.cmd_ch.get_one()
                if cmd is None:
                    sleep_time = 1
                    time.sleep(sleep_time)
                    logger.debug(f"No command found in spawner queue. Sleeping for {sleep_time} seconds.")
                    continue
                # directly ack the messages from the command channel; problems generated from starting pods are
                # handled downstream; e.g., by setting the pod to an ERROR state; command messages should not be re-queued
                try:
                    ack_fn()
                except Exception as e:
                    logger.error(f"Spawner got an exception trying to ack command: {cmd}. "
                                f"Exception type: {type(e).__name__}. Exception: {e}")
                try:
                    executor.submit(self.process, cmd)
                except Exception as e:
                    logger.error(f"Spawner got an exception trying to process cmd: {cmd}. "
                                f"Exception type: {type(e).__name__}. Exception: {e}")
            except Exception as e:
                logger.error(f"Spawner got an exception trying to get a command from the command channel. Exception: {e}")

    def process(self, cmd):
        """Main spawner method for processing a command from the CommandChannel."""
        logger.info(f"top of process; cmd: {cmd}")
        object_id = cmd["object_id"]
        object_type = cmd["object_type"]
        tenant_id = cmd["tenant_id"]
        site_id = cmd["site_id"]
        resolved_secrets = cmd.get("resolved_secrets", {})

        match object_type:
            case "pod":
                spawn_pod(object_id, tenant_id, site_id, resolved_secrets)
            case "volume":
                spawn_pvc(object_id, tenant_id, site_id)
            case _:
                logger.critical(f"Got spawner message with object_type not in 'pod' or 'volume'. Got: {object_type}")

def spawn_pod(pod_id, tenant_id, site_id, resolved_secrets=None):
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
        logger.debug(f"Spawner found pod NOT in REQUESTED status as expected. status: {status}. Returning and ignoring command.")
        return

    status_requested = getattr(pod, 'status_requested', '')
    if not status_requested == ON:
        logger.debug(f"Spawner found pod not requesting ON as expected. status_requested: {status_requested}. Returning and ignoring command.")
        return

    logger.debug(f"Spawner found pod in REQUESTED status as expected. status: {status}. status_requested: {status_requested}")
    # Pod status was REQUESTED and status_requested was ON; moving on to SPAWNER_SETUP ----
    pod.status = SPAWNER_SETUP
    pod.db_update(f"spawner confirmed SPAWNER_REQUESTED bit, set status to SPAWNER_SETUP", tenant=tenant_id, site=site_id)
    logger.debug(f"spawner has updated pod status to SPAWNER_SETUP")

    try:
        start_generic_pod(pod, revision=1, resolved_secrets=resolved_secrets or {})
    except Exception as e:
        logger.critical(f"Got error when creating pod. Running graceful_rm_pod. e: {e}")
        graceful_rm_pod(pod, f"spawner got error when creating pod, set status to DELETING")
        return

    # If we get to this point we can update pod status
    pod.status = CREATING
    pod.db_update(f"spawner set status to CREATING", tenant=tenant_id, site=site_id)
    logger.debug(f"spawner has updated pod status to CREATING")

def spawn_pvc(volume_id, tenant_id, site_id):
    # Get spawn_pvc while in spawner. Expect REQUESTED. If status_requested = OFF then request was started while waiting
    # for command to startup in queue. In that case, we simply abort and wait for health to delete pod.
    try:
        volume = Volume.db_get_with_pk(volume_id, tenant=tenant_id, site=site_id)
    except Exception as e:
        msg = f"Exception in spawner trying to retrieve Volume object from store. Aborting. Exception: {e}"
        logger.error(msg)
        return
    
    status = getattr(volume, 'status', '')
    if not status == REQUESTED:
        logger.debug(f"Spawner found volume NOT in REQUESTED status as expected. status: {status}. Returning and ignoring command.")
        return

    # Volume status was REQUESTED; moving on to SPAWNER_SETUP ----
    volume.status = SPAWNER_SETUP
    volume.db_update()
    logger.debug(f"spawner has updated volume status to SPAWNER_SETUP")

    try:
        create_pvc(name = volume.k8_name)
    except Exception as e:
        logger.critical(f"Got error when creating volume. Running graceful_rm_volume. e: {e}")
        graceful_rm_volume(volume)
        return

    # If we get to this point we can update volume status
    volume.status = CREATING
    volume.db_update()
    logger.debug(f"spawner has updated volume status to CREATING")


def main():
    # todo - find something more elegant
    # Ensure Mongo can connect.
    msg = "Spawner started. Connecting to rabbitmq..."
    logger.debug(msg)
    # Start spawner
    idx = 0
    while idx < 30:
        try:
            time.sleep(4)
            sp = Spawner()
            logger.info("Spawner made connection to rabbit, entering main loop")
            sp.run()
        except (rabbitpy.exceptions.ConnectionException, RuntimeError, rabbitpy.exceptions.ConnectionClosed, Exception):
            # rabbit seems to take a few seconds to come up
            logger.info(f"Attempt to connect to rabbit again. idx {idx} of 20.")
            time.sleep(5)
            idx += 1
    logger.critical("spawner could not connect to rabbitMQ. Shutting down!")

if __name__ == '__main__':
    main()

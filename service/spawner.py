import json
import os
import time

import rabbitpy
from concurrent.futures import ThreadPoolExecutor
from codes import BUSY, ERROR, SPAWNER_SETUP, CREATING_CONTAINER, UPDATING_STORE, READY, \
    REQUESTED, SHUTDOWN_REQUESTED, SHUTTING_DOWN
from health import graceful_rm_pod
from models import Pod
from threading import Thread
from channels import CommandChannel
#from health import get_worker
from kubernetes_utils import create_container, create_service, KubernetesError
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
logger = get_logger(__name__)


class SpawnerException(BaseTapisError):
    """Error with spawner."""
    pass

def start_neo4j_pod(pod, revision: int):
    logger.debug(f"Attempting to start neo4j pod; name: {pod.k8_name}; revision: {revision}")

    # Init new user/pass https://neo4j.com/labs/apoc/4.1/operational/init-script/
    container = {
        "name": pod.k8_name,
        "revision": revision,
        "image": "neo4j",
        "command": [
            '/bin/bash',
            '-c',
            ('export NEO4J_dbms_default__advertised__address=$(hostname -f) && '
            'export NEO4J_causalClustering_discoveryAdvertisedAddress=$(hostname -f)::5000 && '
            'export NEO4J_causalClustering_transactionAdvertisedAddress=$(hostname -f):6000 && '
            'export NEO4J_causalClustering_raftAdvertisedAddress=$(hostname -f):7000 && '
            'exec /docker-entrypoint.sh "neo4j"')
        ],
        "ports_dict": {
            "discovery": 5000,
            "tx": 6000,
            "raft": 7000,
            "browser": 7474,
            "bolt": 7687
        },
        "environment": {
            "NEO4JLABS_PLUGINS": [],
            "NEO4J_USERNAME": pod.k8_name,
            "NEO4J_PASSWORD": "adminadmin",
            "NEO4J_causalClustering_initialDiscoveryMembers": "neo4j-core-0.neo4j.default.svc.cluster.local:5000,neo4j-core-1.neo4j.default.svc.cluster.local:5000,neo4j-core-2.neo4j.default.svc.cluster.local:5000",
            "NEO4J_dbms_security_auth__enabled": "false",
            "NEO4J_dbms_mode": "CORE",
            "NEO4J_dbms_memory_heap_max__size": "3g",
            "NEO4J_dbms_memory_heap_initial__size": "2g",
            "NEO4J_apoc_import_file_enabled": "true",
            "NEO4J_apoc_export_file_enabled": "true",
            "NEO4J_apoc_initializer_system": "CREATE USER name SET ENCRYPTED PASSWORD 1,hash,salt" #SHA256 #create user dummy set password 'abc'"
        },
        "mounts": [],
        "mem_limit": "4G",
        "max_cpus": "1000",
        "user": None
    }

    # Change pod status
    pod.status = CREATING_CONTAINER
    pod.db_update()
    logger.debug(f"spawner has updated pod status to CREATING_CONTAINER")

    # Create init_container, container, and service.
    create_container(**container)
    create_service(name = pod.k8_name, ports_dict = container["ports_dict"])

    # TODO: Change caddy api bit.


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
        pod_name = cmd["pod_name"]
        tenant_id = cmd["tenant_id"]
        site_id = cmd["site_id"]

        # Get pod while in spawner. Expected REQUESTED (Maybe SUBMITTED?). If SHUTDOWN_REQUESTED then request
        # was started while waiting for command to startup in queue.
        ###
        # if the worker was sent a delete request before spawner received this message to create the worker,
        # the status will be SHUTDOWN_REQUESTED, not REQUESTED. in that case, we simply abort and remove the
        # worker from the collection.
        try:
            pod = Pod.db_get_with_pk(pod_name, tenant=tenant_id, site=site_id)
        except Exception as e:
            msg = f"Exception in spawner trying to retrieve pod object from store. Aborting. Exception: {e}"
            logger.error(msg)
            return
        
        status = getattr(pod, 'status', None)
        if not status == REQUESTED:
            logger.debug(f"Spawner found pod NOT in REQUESTED status as expected. status: {status}. Returning and not processing command.")
            return
        
        # Pod status was REQUESTED; moving on to SPAWNER_SETUP ----
        pod.status = SPAWNER_SETUP
        pod.db_update()
        logger.debug(f"spawner has updated pod status to SPAWNER_SETUP")

        # TODO: If database_type == neo4j, then fn. How to generalize?
        try:
            start_neo4j_pod(pod=pod, revision=1)
        except Exception as e:
            msg = f"Got error when creating pod. Running graceful_rm_pod. e: {e}"
            graceful_rm_pod(pod)

def main():
    # todo - find something more elegant
    # Ensure Mongo can connect.
    msg = "Spawner started. Connecting to rabbitmq..."
    logger.debug(msg)
    print(msg)
    # Start spawner
    idx = 0
    while idx < 3:
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

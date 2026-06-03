"""
Remote health service for Tapis Pods
This attaches to the exposed central services.
"""
"""
Does the following:
1. Go through running k8 pods
  a. Remove pods that are not in the database (dangling)
  b. Else update the database based on information from the pod
    - Update logs
    - Update status
      - If pod is in Completed, put in Completed
      - If pod is in Error, put in Error
      - If pod is in Running, put in Running
    - Update status_container
  c. Clean up dangling services.

2. Go through database. 
  a. Deletes pods with status_requested = OFF
     i. Delete pods with status_requested = OFF. Then sets status_requested = ON.
  b. Deletes pods in error status. (Maybe not?)
  c. Updates pod status.
  d. Ensures all database pods exist (For this site)
  e. Ensures all pods that exist are in the database.
    i. Also checks that pods are healthy and communicating

3. Enforce ttl for pods.

4. Look through S3? Prune old things?/Prune non-existing pods.

5. Check that spawner is alive? Create if needed?
  a. Maybe have a warning slack message if none available?

Running mode, either:
1. Periodically run script.
2. Always keep running in big loop.
"""

import time
import os
from scale_utils import setup_tailscale, add_k8_pods_to_tailscale
from kubernetes_utils import check_k8s_access_and_roles
from __init__ import t
from tapisservice.logs import get_logger
from tapisservice.config import conf
import psycopg2  # For Postgres
import pika      # For RabbitMQ

logger = get_logger(__name__)

# Load environment/config
ENV_NAME = conf.get('envname', os.environ.get('ENV_NAME', 'default_env'))
RABBITMQ_URL = os.environ.get('RABBITMQ_URL', conf.get('rabbitmq_url', 'amqp://guest:guest@localhost:5672/'))
POSTGRES_URL = os.environ.get('POSTGRES_URL', conf.get('postgres_url', 'postgresql://user:pass@localhost:5432/db'))

def main():
    logger.info(f"Starting remote health service for env: {ENV_NAME}")

    # Using tapipy we get cluster bootstrap info
    #cluster = t.clusters.getClusterByName(ENV_NAME)
    cluster = f"https://tacc.develop.tapis.io/pods/clusters"

    # Connect to Tailscale
    result = setup_tailscale()

    # Connect to RabbitMQ
    try:
        rabbitmq_conn = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        logger.info("Connected to RabbitMQ.")
    except Exception as e:
        logger.critical(f"Failed to connect to RabbitMQ: {e}")
        pass

    # Connect to Postgres
    try:
        pg_conn = psycopg2.connect(POSTGRES_URL)
        logger.info("Connected to Postgres.")
    except Exception as e:
        logger.critical(f"Failed to connect to Postgres: {e}")
        pass

    # Check Kubernetes access and roles
    if not check_k8s_access_and_roles():
        logger.critical("Kubernetes access/roles insufficient. Exiting...")
        pass

    # Main loop for health and spawning logic
    while True:
        # TODO: Add health checks and spawner logic here
        logger.info("Health/spawner loop running...")
        time.sleep(45)
    # Add Kubernetes pods to Tailscale
    # add_k8_pods_to_tailscale()

    # # Main health loop
    # while True:
    #     logger.info("Running health checks...")
    #     k8_pods = get_current_k8_pods()
    #     k8_services = get_current_k8_services()

    #     logger.info(f"Found {len(k8_pods)} pods and {len(k8_services)} services.")
    #     time.sleep(60)

if __name__ == '__main__':
    main()

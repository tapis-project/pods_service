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
from scale_utils import setup_tailscale, add_k8_pods_to_tailscale
from kubernetes_utils import check_k8s_access_and_roles
from tapisservice.logs import get_logger

logger = get_logger(__name__)

def main():
    logger.info("Starting remote health service...")

    # Connect to Tailscale
    result = setup_tailscale()

    # Check Kubernetes access and roles
    if not check_k8s_access_and_roles():
        logger.critical("Kubernetes access/roles insufficient. Exiting.")
        return

    while True:
        print('hey')
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

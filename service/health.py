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
import random
from datetime import datetime, timedelta
from channels import CommandChannel
from kubernetes import client, config
from kubernetes_utils import get_current_k8_services, get_current_k8_pods, rm_container, rm_pvc, \
    rm_service, KubernetesError, get_k8_logs, get_traefik_logs, list_all_containers, run_k8_exec, \
    list_configmaps_by_prefix, delete_configmap, NAMESPACE, get_pod_k8_events
from codes import AVAILABLE, DELETING, STOPPED, ERROR, REQUESTED, COMPLETE, RESTART, ON, OFF
from stores import pg_store, SITE_TENANT_DICT
from models_pods import Pod, PodBaseFull
from models_volumes import Volume
from models_snapshots import Snapshot
from models_templates_utils import combine_pod_and_template_recursively
from kubernetes_templates import ensure_pod_configmaps, ephemeral_configmap_name
from secret_utils import resolve_secret_map
from models_volume_mounts_utils import validate_volume_mounts_on_start
from models_pod_log_runs import PodLogRun
from models_traffic import TrafficLog
from traffic_utils import parse_traefik_access_logs, entry_to_traffic_record
from log_archive_utils import truncate_if_oversized, maybe_archive_old_runs
from psycopg2 import ProgrammingError
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger

logger = get_logger(__name__)

# Per-pod cursor tracking the latest ts already ingested from Traefik access logs.
# Keyed by "site:tenant:pod_id". Reset on process restart (safe — we deduplicate by ts).
_traefik_log_cursor: dict[str, 'datetime'] = {}


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def rm_pod(k8_name):
    container_exists = True
    service_exists = True
    try:
        rm_container(k8_name)
    except KubernetesError:
        # container not found
        container_exists = False
        pass
    try:
        rm_service(k8_name)
    except KubernetesError:
        # service not found
        service_exists = False
        pass
    
    # Clean up any ConfigMaps associated with this pod (ephemeral volume mounts)
    rm_pod_configmaps(k8_name)

    return container_exists, service_exists


def rm_pod_configmaps(k8_name):
    """
    Remove all ConfigMaps associated with a pod (created for ephemeral volume mounts).
    ConfigMap names are prefixed with the pod's k8_name.
    """    
    try:
        # List ConfigMaps that start with the pod's k8_name
        configmap_names = list_configmaps_by_prefix(k8_name.lower(), namespace=NAMESPACE)
        
        for cm_name in configmap_names:
            try:
                delete_configmap(cm_name, namespace=NAMESPACE)
                logger.info(f"Deleted ConfigMap {cm_name} for pod {k8_name}")
            except Exception as e:
                logger.warning(f"Failed to delete ConfigMap {cm_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list ConfigMaps for pod {k8_name}: {e}")

def rm_volume(k8_name):
    volume_exists = True
    try:
        rm_pvc(k8_name)
    except KubernetesError:
        # volume not found
        volume_exists = False
        pass

    return volume_exists

def graceful_rm_pod(pod, log=None):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete pod, delete service, and change traefik to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    try:
        logger.info(f"Top of shutdown pod for pod: {pod.k8_name}")
        # Finalize the active log run before removing the pod
        try:
            PodLogRun.finalize_run(pod.pod_id, pod.tenant_id, pod.site_id)
        except Exception as e:
            logger.warning(f"Could not finalize log run for pod {pod.pod_id}: {e}")
        # Change pod status to SHUTTING DOWN
        pod.status = DELETING
        pod.db_update(log, user_update=False)
        logger.debug(f"spawner has updated pod status to DELETING")

        return rm_pod(pod.k8_name)
    except Exception as e:
        logger.error(f"Failed to gracefully remove pod {pod.k8_name}")
        raise

def graceful_rm_volume(volume):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete volume, delete volume, and change traefik to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    try:
        logger.info(f"Top of shutdown volume for volume: {volume.k8_name}")
        # Change volume status to SHUTTING DOWN
        volume.status = DELETING
        volume.db_update()
        logger.debug(f"spawner has updated volume status to DELETING")

        return rm_volume(volume.k8_name)
    except Exception as e:
        logger.error(f"Failed to gracefully remove volume {volume.k8_name}")
        raise

def check_k8_pods(k8_pods):
    """
    Check the health of Kubernetes pods.
    Only for the site specified in conf.site_id.
    Each site should get it's own health pod.
    Go through live containers first as base "truth". Set database from that info. (error if needed, update statuses)
    
    Args:
        k8_pods (list): A list of Kubernetes pods to check.

    Returns:
        None
    """

    # Check each pod.
    for k8_pod in k8_pods:
        logger.info(f"Checking pod health for pod_id: {k8_pod['pod_id']}")

        # Check if pod is found in database.
        pod = Pod.db_get_with_pk(k8_pod['pod_id'], k8_pod['tenant_id'], k8_pod['site_id'])
        # We've found a pod without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 pod without any database entry. Deleting. Pod: {k8_pod['k8_name']}")
            rm_pod(k8_pod['k8_name'])
            continue

        # Wrap entire pod processing in try/except to catch validation errors during field assignments.
        # This can happen when a pod references a template that no longer exists - pydantic validates
        # on every field assignment and will raise ValidationError if template lookup fails.
        try:
            pre_health_pod = pod.copy()

            # Found pod in db.
            # Add last_health_check attr.
            # TODO We could try and make a get to the pod to check if it's actually alive.

            k8_pod_phase = k8_pod['pod_info'].status.phase
            start_time = k8_pod['pod_info'].status.start_time
            if start_time:
                start_time = start_time.isoformat().replace('+00:00', '.000000')
            else:
                start_time = None

            status_container = {"phase": k8_pod_phase,
                                "start_time": start_time,
                                "message": ""}
            
            # Get pod container state
            # We try to get c_state. c_state when pending is None for a bit.
            try:
                c_state = k8_pod['pod_info'].status.container_statuses[0].state
            except:
                c_state = None
            logger.debug(f'state: {c_state}')

            # We don't run these checks on pods with status_requested = OFF as they're going through tear down stuff.
            if pod.status_requested in [OFF, RESTART]:
                continue

            # This is actually bad. Means the pod has stopped, which shouldn't be the case.
            # We'll put pod in error state with message.
            if k8_pod_phase == "Succeeded":
                logger.warning(f"Kube pod in succeeded phase.")
                status_container['message'] = "Pod phase in Succeeded, putting in COMPLETE status."
                pod.status_container = status_container
                pod.status = COMPLETE
                # We update if there's been a change.
                if pod != pre_health_pod:
                    pod.db_update(f"health found pod in succeeded, set status to COMPLETE", user_update=False)
                continue
            elif k8_pod_phase in ["Running", "Pending", "Failed"]:
                # Check if container running or in error state
                # Container can be in waiting state due to ContainerCreating ofc
                if c_state:
                    if c_state.waiting and c_state.waiting.reason != "ContainerCreating":
                        reason = c_state.waiting.reason or "Unknown"
                        detail = c_state.waiting.message or ""
                        logger.error(f"Kube pod in waiting/error state. reason={reason}; detail={detail}")
                        # Fetch k8s events for richer context (ImagePullBackOff, FailedMount, etc.)
                        k8_events = get_pod_k8_events(k8_pod['k8_name'])
                        event_lines = [
                            f"[{e['reason']}] {e['message']}" for e in k8_events[:5]
                            if e.get('message')
                        ]
                        event_summary = " | ".join(event_lines)
                        full_msg = f"{reason}: {detail}" if detail else reason
                        if event_summary:
                            full_msg = f"{full_msg} — {event_summary}"
                        status_container['message'] = full_msg
                        pod.status_container = status_container
                        pod.status = ERROR
                        if pod != pre_health_pod:
                            pod.db_update(f"health: pod waiting error — {full_msg[:300]}", user_update=False)
                        continue
                    elif c_state.terminated:
                        reason = c_state.terminated.reason or ""
                        detail = c_state.terminated.message or ""
                        exit_code = getattr(c_state.terminated, 'exit_code', None)
                        logger.error(f"Kube pod terminated. reason={reason}; exit_code={exit_code}; detail={detail}")
                        # Fetch k8s events — captures OOMKilled, BackOff, FailedMount details
                        k8_events = get_pod_k8_events(k8_pod['k8_name'])
                        event_lines = [
                            f"[{e['reason']}] {e['message']}" for e in k8_events[:5]
                            if e.get('message')
                        ]
                        event_summary = " | ".join(event_lines)
                        parts = []
                        if reason:
                            parts.append(reason)
                        if exit_code is not None:
                            parts.append(f"exit={exit_code}")
                        if detail:
                            parts.append(detail)
                        full_msg = " ".join(parts) if parts else "Container terminated"
                        if event_summary:
                            full_msg = f"{full_msg} — {event_summary}"
                        status_container['message'] = full_msg
                        pod.status_container = status_container
                        pod.status = ERROR
                        if pod != pre_health_pod:
                            logs = get_k8_logs(k8_pod['k8_name'])
                            pod.logs = logs
                            pod.db_update(f"health: pod terminated — {full_msg[:300]}", user_update=False)
                        continue
                    elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
                        logger.info(f"Kube pod in waiting state, still creating container.")
                        status_container['message'] = "Pod is still initializing."
                        pod.status_container = status_container
                        # We update if there's been a change.
                        if pod != pre_health_pod:
                            pod.db_update(user_update=False) # no logs needed, spawner already states it's being put in creating.
                        continue
                    elif c_state.running:
                        status_container['message'] = "Pod is running."
                        # Enrich with readiness and restart count (zero-cost: same object already read above)
                        try:
                            cs = k8_pod['pod_info'].status.container_statuses[0]
                            status_container['ready'] = bool(cs.ready)
                            status_container['restart_count'] = int(cs.restart_count or 0)
                        except Exception:
                            pass
                        pod.status_container = status_container
                        # This is the first time pod is in AVAILABLE. Update start_instance_ts.
                        if pod.status != AVAILABLE:
                            pod.start_instance_ts = datetime.utcnow()
                            pod.status = AVAILABLE
                            # Start a new log run for this pod instance
                            try:
                                PodLogRun.finalize_run(pod.pod_id, pod.tenant_id, pod.site_id)
                                PodLogRun.get_or_create_active_run(pod.pod_id, pod.tenant_id, pod.site_id)
                                maybe_archive_old_runs(pod.pod_id, pod.tenant_id, pod.site_id)
                            except Exception as _lr_e:
                                logger.warning(f"Log run init failed for pod {pod.pod_id}: {_lr_e}")

                        if pod.start_instance_ts:
                            # This will set time_to_stop_ts the first time pod is available and if
                            # time_to_stop_instance or time_to_stop_default is updated.
                            #
                            # IMPORTANT: If pod uses a template, derive the time_to_stop values from
                            # the merged template, as templates can set time_to_stop_default/-1 etc.
                            time_to_stop_instance = pod.time_to_stop_instance
                            time_to_stop_default = pod.time_to_stop_default
                            
                            if pod.template:
                                try:
                                    # Derive merged pod to get template's time_to_stop values
                                    pod_copy = PodBaseFull(**pod.dict().copy())
                                    derived_pod = combine_pod_and_template_recursively(
                                        pod_copy, pod.template, tenant=pod.tenant_id, site=pod.site_id
                                    )
                                    # Use template's values if pod didn't explicitly set them
                                    if 'time_to_stop_instance' not in (pod.modified_fields or []):
                                        time_to_stop_instance = derived_pod.time_to_stop_instance
                                    if 'time_to_stop_default' not in (pod.modified_fields or []):
                                        time_to_stop_default = derived_pod.time_to_stop_default
                                    logger.debug(f"Pod {pod.pod_id} using template time_to_stop: instance={time_to_stop_instance}, default={time_to_stop_default}")
                                except Exception as e:
                                    logger.warning(f"Failed to derive template time_to_stop for pod {pod.pod_id}: {e}")
                            
                            if isinstance(time_to_stop_instance, int):
                                # If set to -1, we don't do ttl.
                                if not time_to_stop_instance == -1:
                                    pod.time_to_stop_ts = pod.start_instance_ts + timedelta(seconds=time_to_stop_instance)
                            else:
                                # If set to -1, we don't do ttl.
                                if not time_to_stop_default == -1:
                                    pod.time_to_stop_ts = pod.start_instance_ts + timedelta(seconds=time_to_stop_default)
                        # We update if there's been a change.
                        if pod != pre_health_pod:
                            pod.db_update(f"health set status to AVAILABLE", user_update=False)
                else:
                    # Not sure if this is possible/what happens here.
                    # There is definitely an Error state. Can't replicate locally yet.
                    logger.critical(f"NO c_state. {k8_pod['pod_info'].status}")

            # Getting here means pod is running. Store logs now.
            logs = get_k8_logs(k8_pod['k8_name'])
            if logs:
                logs = logs.replace('\x00', '')
                logs = truncate_if_oversized(logs)
            if pod.logs != logs:
                pod.logs = logs
                try:
                    pod.db_update(user_update=False)  # just adding logs, no action_logs needed.
                except Exception as e:
                    logger.error(f"Error updating pod logs: {e}", exc_info=True)
            # Mirror logs to the active PodLogRun
            try:
                run = PodLogRun.get_active_run(pod.pod_id, pod.tenant_id, pod.site_id)
                if run and run.logs != logs:
                    run.logs = logs
                    run.log_size_bytes = len(logs.encode('utf-8', errors='replace')) if logs else 0
                    run.db_update(tenant=pod.tenant_id, site=pod.site_id)
            except Exception as e:
                logger.warning(f"Could not update log run for pod {pod.pod_id}: {e}")

        except Exception as e:
            # Catch validation errors that occur during field assignments (e.g., when a pod references
            # a template that no longer exists). Log the error and continue checking other pods.
            logger.error(f"Error processing pod {k8_pod['pod_id']} in health check: {e}", exc_info=True)
            continue

def check_k8_services():
    # This is all for only the site specified in conf.site_id.
    # Each site should get it's own health pod.
    # Go through live containers first as it's "truth". Set database from that info. (error if needed, update statuses)
    k8_services = get_current_k8_services() # Returns {service_info, site, tenant, pod_id}

    # Check each service.
    for k8_service in k8_services:
        logger.info(f"Checking service health for pod_id: {k8_service['pod_id']}")

        # Check for found service in database.
        pod = Pod.db_get_with_pk(k8_service['pod_id'], k8_service['tenant_id'], k8_service['site_id'])
        # We've found a service without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 service without any database entry. Deleting. Service: {k8_service['k8_name']}")
            rm_pod(k8_service['k8_name'])
            continue

def check_db_pods(k8_pods):
    """Go through database for all tenants in this site. Delete/Create whatever is needed.
    """
    all_pods = []
    stmt = select(Pod)
    failed_tenants = []
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        try:
            all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)
        except ProgrammingError as e:
            logger.warning(f"Tenant: {tenant} not found in database. Skipping.")
            failed_tenants.append(tenant)
            continue
    # If > 2/20 tenants fail we'll skip, expecting up to two new tenants.
    # Pods needs to restart after new tenants are added for their database to be created.
    # It should not break currently working health though. Thus skipping if only a small portion of tenants fail.
    if len(failed_tenants) >= 2:
        logger.critical(f"More than 2 tenants failed to connect to database. Possible error or waiting for startup. Shutting down.")
        return


    ### Go through all pod entries in the database
    for pod in all_pods:
        # Wrap pod processing in try/except to catch validation errors during field assignments.
        # This can happen when a pod references a template that no longer exists.
        try:
            ### Delete pods with status_requested = OFF or RESTART
            if pod.status_requested in [OFF, RESTART] and pod.status != STOPPED:
                logger.info(f"pod_id: {pod.pod_id} found with status_requested: {pod.status_requested} and not STOPPED. Gracefully shutting pod down.")
                container_exists, service_exists = graceful_rm_pod(pod, f"health found running {pod.status_requested} pod, set status to DELETING") # SHOULD ONLY LOG ONCE!!!
                # if container and service not alive. Update status to STOPPED. UPDATE RESTART to ON.
                if not container_exists and not service_exists:
                    logger.info(f"pod_id: {pod.pod_id} found with container and service stopped. Moving to status = STOPPED.")
                    pod.status = STOPPED
                    pod.start_instance_ts = None
                    pod.time_to_stop_ts = None
                    pod.time_to_stop_instance = None
                    pod.status_container = {}
                    if pod.status_requested == RESTART:
                        logger.info(f"pod_id: {pod.pod_id} in RESTART. Now in STOPPED, so switching status_requested back to ON.")
                        pod.status_requested = ON
                        pod.db_update(f"health set status to STOPPED, set to ON", user_update=False)
                    else:
                        pod.db_update(f"health set status to STOPPED", user_update=False)

            ### DB entries without a running pod should be updated to STOPPED.
            if pod.status_requested in ['ON'] and pod.status in [AVAILABLE, DELETING, REQUESTED]:
                k8_pod_found = False
                for k8_pod in k8_pods:
                    if pod.pod_id in k8_pod['pod_id']:
                        k8_pod_found = True

                if not k8_pod_found:
                    # Check action_logs for proper course of action
                    if not pod.action_logs:
                        # logs can be empty if an admin manually deleted them or if we ran a db migration. Accounting for that here.
                        log_str = "No action logs found. Expecting to go to 'else' to be shutdown"
                        time_difference = timedelta(minutes=5) # This line exists to stop linter complaints
                    else:
                        # We let pods in CREATING or REQUESTED have timeout of 3 minutes before we stop the pod
                        # and let health try again. We check time based on pod action_logs.
                        # Get the most recent log and split on ': ' to get the time, log_str
                        log_time_str, log_str = pod.action_logs[-1].split(': ', maxsplit=1)
                        most_recent_log_time = datetime.strptime(log_time_str, '%y/%m/%d %H:%M')
                        time_difference = datetime.utcnow() - most_recent_log_time

                    # We check pod logs to see if pod is in a state where it should have a 3 minute timeout
                    if "set status to REQUESTED" in log_str or \
                        "set status to CREATING" in log_str or \
                        "Pod object created by" in log_str:
                        # If pod has been in state for 3 minutes we'll stop it (Note 3+1 allows a 1 minute buffer as a log can be written at :59 seconds)
                        if time_difference > timedelta(minutes=3+1):
                            initial_pod_status = pod.status
                            logger.info(f"pod_id: {pod.pod_id} found with no running pods and in {initial_pod_status} for 3 minutes. Setting status = STOPPED")
                            pod.status = STOPPED
                            pod.start_instance_ts = None
                            pod.time_to_stop_ts = None
                            pod.time_to_stop_instance = None
                            pod.status_container = {}
                            pod.db_update(f"health found no running pod and status = {initial_pod_status} for 3 minutes, stalled. Setting status = STOPPED", user_update=False)
                        else:
                            # Not stalled yet, we just continue
                            continue
                    else:                 
                        logger.info(f"pod_id: {pod.pod_id} found with no running pods. Setting status = STOPPED.")
                        pod.status = STOPPED
                        pod.start_instance_ts = None
                        pod.time_to_stop_ts = None
                        pod.time_to_stop_instance = None
                        pod.status_container = {}
                        pod.db_update(f"health found no running pod, set status to STOPPED", user_update=False)

            ### Sets pods to status_requested = OFF when current time > time_to_stop_ts.
            if pod.status_requested in ['ON'] and pod.time_to_stop_ts and pod.time_to_stop_ts < datetime.utcnow():
                logger.info(f"pod_id: {pod.pod_id} time_to_stop trigger passed. Current time: {datetime.utcnow()} > time_to_stop_ts: {pod.time_to_stop_ts}")
                pod.status_requested = OFF
                pod.db_update(f"health set pod to OFF due to time_to_stop trigger", user_update=False)
            
            ### Start pods here by putting command setting status="REQUESTED", if status_requested = ON and status = STOPPED.
            if pod.status_requested in ['ON', RESTART] and pod.status == STOPPED:
                logger.info(f"pod_id: {pod.pod_id} found status_requested: {pod.status_requested} and STOPPED. Starting.")
                original_pod_status = pod.status_requested
                if pod.status_requested == RESTART:
                    logger.info(f"pod_id: {pod.pod_id} in RESTART and STOPPED, so switching status_requested back to ON.")
                    pod.status_requested = ON

                # Derive template info first (needed for both volume and secret validation)
                derived_pod = None
                if pod.template:
                    try:
                        pod_copy = PodBaseFull(**pod.dict().copy())
                        derived_pod = combine_pod_and_template_recursively(
                            pod_copy, pod.template, tenant=pod.tenant_id, site=pod.site_id
                        )
                    except Exception as e:
                        logger.error(f"Failed to derive template for pod {pod.pod_id}: {e}")
                        pod.status = ERROR
                        pod.db_update(f"health failed to derive template: {str(e)}", user_update=False)
                        continue

                # Validate volume mounts before starting:
                # - Check mounted_by users still have permission on the pod
                # - Check mounted_by users still have READ permission on volumes/snapshots
                derived_volume_mounts = getattr(derived_pod, 'volume_mounts', {}) if derived_pod else (pod.volume_mounts or {})
                if derived_volume_mounts:
                    vm_errors = validate_volume_mounts_on_start(
                        volume_mounts=derived_volume_mounts,
                        pod_permissions=pod.get_permissions(),
                        tenant=pod.tenant_id,
                        site=pod.site_id
                    )
                    if vm_errors:
                        logger.error(f"Volume mount validation failed for pod {pod.pod_id}: {'; '.join(vm_errors)}")
                        pod.status = ERROR
                        pod.db_update(f"health volume mount validation failed: {'; '.join(vm_errors)}", user_update=False)
                        continue

                # Resolve secrets at central health layer before sending to spawner
                # This allows edge spawners to work without direct SK access
                resolved_secrets = {}
                
                # Get merged secret_map (use derived_pod if already computed)
                if derived_pod:
                    merged_secret_map = getattr(derived_pod, 'secret_map', {}) or {}
                else:
                    merged_secret_map = pod.secret_map or {}
                
                if merged_secret_map:
                    try:
                        # Resolve secrets without actor - uses owner from notation
                        # Security: secret owner is validated against DB's added_by field
                        resolved_secrets, secret_errors = resolve_secret_map(
                            merged_secret_map,
                            site_id=pod.site_id,
                            tenant_id=pod.tenant_id,
                            pod_id=pod.pod_id,
                            pod=pod
                        )
                        if secret_errors:
                            logger.error(f"Failed to resolve secrets for pod {pod.pod_id}: {'; '.join(secret_errors)}")
                            # Set to error state rather than failing silently
                            pod.status = ERROR
                            pod.db_update(f"health failed to resolve secrets: {'; '.join(secret_errors)}", user_update=False)
                            continue
                    except Exception as e:
                        logger.error(f"Exception resolving secrets for pod {pod.pod_id}: {e}")
                        pod.status = ERROR
                        pod.db_update(f"health exception resolving secrets: {str(e)}", user_update=False)
                        continue

                pod.status = REQUESTED
                pod.db_update(f"health found {original_pod_status} pod set to STOPPED, set status to REQUESTED", user_update=False)

                # Send command to start new pod
                ch = CommandChannel(name=pod.site_id)
                ch.put_cmd(object_id=pod.pod_id,
                           object_type="pod",
                           tenant_id=pod.tenant_id,
                           site_id=pod.site_id,
                           resolved_secrets=resolved_secrets)
                ch.close()
                logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")

        except Exception as e:
            # Catch validation errors that occur during field assignments (e.g., when a pod references
            # a template that no longer exists). Log the error and continue checking other pods.
            logger.error(f"Error processing pod {pod.pod_id} in check_db_pods: {e}", exc_info=True)
            continue


_last_configmap_reconcile = 0
CONFIGMAP_RECONCILE_INTERVAL = 60  # seconds


def reconcile_configmaps(k8_pods):
    """
    Proactively verify that ConfigMaps backing ephemeral volume mounts exist
    for all running pods. Regenerates any missing ConfigMaps so K8s can
    remount them if a pod is rescheduled to another node.

    Handles: cluster migration, node failure, accidental CM deletion.
    Throttled to run at most once per CONFIGMAP_RECONCILE_INTERVAL seconds.
    """
    global _last_configmap_reconcile
    now = time.time()
    if now - _last_configmap_reconcile < CONFIGMAP_RECONCILE_INTERVAL:
        return
    _last_configmap_reconcile = now

    logger.debug("reconcile_configmaps: Starting ConfigMap reconciliation check.")

    # Identify pods that are actually running in K8s
    running_pods = []
    for k8_pod in k8_pods:
        try:
            c_state = k8_pod['pod_info'].status.container_statuses[0].state
            if c_state and c_state.running:
                running_pods.append(k8_pod)
        except Exception:
            continue

    if not running_pods:
        return

    # Batch-list all ConfigMaps once (avoids per-mount API calls)
    try:
        all_configmaps = set(
            cm.metadata.name
            for cm in k8.list_namespaced_config_map(namespace=NAMESPACE).items
        )
    except Exception as e:
        logger.error(f"reconcile_configmaps: Failed to list ConfigMaps: {e}")
        return

    for k8_pod in running_pods:
        try:
            pod = Pod.db_get_with_pk(k8_pod['pod_id'], k8_pod['tenant_id'], k8_pod['site_id'])
            if not pod or pod.status != AVAILABLE:
                continue

            # Derive volume_mounts (merge template if applicable)
            derived_pod = pod
            if pod.template:
                try:
                    pod_copy = PodBaseFull(**pod.dict().copy())
                    derived_pod = combine_pod_and_template_recursively(
                        pod_copy, pod.template, tenant=pod.tenant_id, site=pod.site_id
                    )
                except Exception as e:
                    logger.warning(f"reconcile_configmaps: Failed to derive template for pod {pod.pod_id}: {e}")
                    continue

            volume_mounts = getattr(derived_pod, 'volume_mounts', {}) or {}

            # Quick check: are any expected ephemeral ConfigMaps missing?
            any_missing = False
            for mount_path, vol_mount in volume_mounts.items():
                if vol_mount is None:
                    continue
                vtype = vol_mount.get('type', '') if isinstance(vol_mount, dict) else getattr(vol_mount, 'type', '')
                if vtype.lower() != 'ephemeral':
                    continue
                expected_name = ephemeral_configmap_name(pod.k8_name, mount_path)
                if expected_name not in all_configmaps:
                    any_missing = True
                    break

            if not any_missing:
                continue

            # At least one ConfigMap is missing — resolve secrets for interpolation
            logger.warning(f"reconcile_configmaps: Pod {pod.pod_id} has missing ephemeral ConfigMap(s). "
                           "Resolving secrets for regeneration.")

            resolved_secrets = {}
            merged_secret_map = getattr(derived_pod, 'secret_map', {}) or {}
            if merged_secret_map:
                try:
                    resolved_secrets, secret_errors = resolve_secret_map(
                        merged_secret_map,
                        site_id=pod.site_id,
                        tenant_id=pod.tenant_id,
                        pod_id=pod.pod_id,
                        pod=pod,
                    )
                    if secret_errors:
                        logger.error(f"reconcile_configmaps: Secret resolution errors for pod {pod.pod_id}: "
                                     f"{'; '.join(secret_errors)}")
                        continue
                except Exception as e:
                    logger.error(f"reconcile_configmaps: Exception resolving secrets for pod {pod.pod_id}: {e}")
                    continue

            regenerated = ensure_pod_configmaps(
                pod,
                resolved_secrets=resolved_secrets,
                existing_configmaps=all_configmaps,
            )

            if regenerated:
                # Update cached set so subsequent pods in this pass don't re-check
                all_configmaps.update(regenerated)
                pod.db_update(f"health regenerated {len(regenerated)} missing ConfigMap(s): {', '.join(regenerated)}", user_update=False)
                logger.info(f"reconcile_configmaps: Regenerated {len(regenerated)} ConfigMap(s) for pod {pod.pod_id}")

        except Exception as e:
            logger.error(f"reconcile_configmaps: Error processing pod {k8_pod.get('pod_id', '?')}: {e}", exc_info=True)
            continue


def sync_traefik_traffic_logs():
    """Read Traefik access logs and insert new TrafficLog rows for each pod.

    Uses _traefik_log_cursor to skip already-ingested entries (keyed by site:tenant:pod_id).
    Safe to call every health tick — only rows newer than the cursor are inserted.
    """
    global _traefik_log_cursor
    raw = get_traefik_logs(lines=500)
    if not raw:
        logger.warning("sync_traefik_traffic_logs: get_traefik_logs returned empty — traefik pod not found or no stdout.")
        return

    raw_lines = raw.splitlines()
    logger.info(f"sync_traefik_traffic_logs: got {len(raw_lines)} raw lines from traefik pod.")

    entries = parse_traefik_access_logs(raw)
    if not entries:
        logger.warning(f"sync_traefik_traffic_logs: {len(raw_lines)} raw lines but 0 JSON-parseable entries. "
                       f"First line sample: {raw_lines[0][:200] if raw_lines else '(empty)'}")
        return

    # Group new records by (site, tenant) so we can batch-insert per store
    skipped_no_match = 0
    skipped_cursor = 0
    by_store: dict[tuple, list] = {}
    for entry in entries:
        record = entry_to_traffic_record(entry)
        if not record:
            skipped_no_match += 1
            continue
        cursor_key = f"{record['site_id']}:{record['tenant_id']}:{record['pod_id']}"
        cursor_ts = _traefik_log_cursor.get(cursor_key)
        if cursor_ts and record['ts'] <= cursor_ts:
            skipped_cursor += 1
            continue
        key = (record['site_id'], record['tenant_id'])
        by_store.setdefault(key, []).append(record)

    logger.info(f"sync_traefik_traffic_logs: {len(entries)} JSON entries — "
                f"{skipped_no_match} no router match, {skipped_cursor} already ingested, "
                f"{sum(len(v) for v in by_store.values())} new records across {len(by_store)} store(s).")

    if skipped_no_match == len(entries):
        router_names = list({e.get('RouterName', '') for e in entries if e.get('RouterName')})
        logger.warning(f"sync_traefik_traffic_logs: ALL entries skipped — no router matched pods-{{site}}-{{tenant}}-{{pod_id}}@file. "
                       f"Router names seen: {router_names[:10]}")

    from stores import pg_store
    for (site, tenant), records in by_store.items():
        if site not in pg_store or tenant not in pg_store.get(site, {}):
            logger.warning(f"sync_traefik_traffic_logs: no pg_store for site={site} tenant={tenant}, skipping {len(records)} records.")
            continue
        store = pg_store[site][tenant]
        inserted_by_pod: dict[str, 'datetime'] = {}
        for rec in records:
            try:
                log_row = TrafficLog(
                    pod_id=rec['pod_id'],
                    ts=rec['ts'],
                    method=rec['method'],
                    path=rec['path'],
                    status_code=rec['status_code'],
                    duration_ms=rec['duration_ms'],
                    source_ip=rec['source_ip'],
                    username=rec['username'],
                    entry_point=rec['entry_point'],
                    router_name=rec['router_name'],
                    raw_headers=rec['raw_headers'],
                    tenant_id=tenant,
                    site_id=site,
                )
                store.run("add", log_row)
                prev = inserted_by_pod.get(rec['pod_id'])
                if prev is None or rec['ts'] > prev:
                    inserted_by_pod[rec['pod_id']] = rec['ts']
            except Exception as e:
                logger.warning(f"Could not insert traffic log row for pod {rec['pod_id']}: {e}")

        # Advance cursors and purge old rows
        for pod_id, latest_ts in inserted_by_pod.items():
            cursor_key = f"{site}:{tenant}:{pod_id}"
            _traefik_log_cursor[cursor_key] = latest_ts
            try:
                TrafficLog.purge_old(pod_id, tenant, site, keep=1000)
            except Exception as e:
                logger.warning(f"traffic purge_old failed for pod {pod_id}: {e}")


def main():
    # Try and run check_db_pods. Will try for 60 seconds until health is declared "broken".
    logger.info("Top of health. Checking if db's are initialized.")
    idx = 0
    while idx < 12:
        try:
            k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}
            check_db_pods(k8_pods)
            logger.info("Successfully connected to dbs.")
            break
        except Exception as e:
            logger.info(f"Can't connect to dbs yet idx: {idx}. e: {e}") # args: {e.args} # add e.args for more detail
            # Health seems to take a few seconds to come up (due to database creation and api creation)
            # Increment and have a short wait
            idx += 1
            time.sleep(5)
    # Reached end of idx limit
    else:
        logger.critical("Health could not connect to databases. Shutting down!")
        return

    # Main health loop
    while True:
        logger.info(f"Running pods health checks. Now: {time.time()}")
        k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}
        k8_services = get_current_k8_services()

        check_k8_pods(k8_pods)
        check_k8_services()
        check_db_pods(k8_pods)
        reconcile_configmaps(k8_pods)

        # Ingest new Traefik access log entries into traffic_logs table
        try:
            sync_traefik_traffic_logs()
        except Exception as e:
            logger.warning(f"sync_traefik_traffic_logs error: {e}")

        ### Have a short wait
        time.sleep(3)


if __name__ == '__main__':
    main()

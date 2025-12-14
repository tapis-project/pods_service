import re
import json
from fastapi import APIRouter
from models_pods import Pod, UpdatePod, PodResponse, Password, PodDeleteResponse, PodsFinalResponse, PodBaseFull
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from models_templates_utils import combine_pod_and_template_recursively, get_template_merged_secret_map, validate_pod_secret_map_against_template

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}

@router.put(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="update_pod",
    operation_id="update_pod",
    response_model=PodResponse)
async def update_pod(pod_id, update_pod: UpdatePod):
    """
    Update a pod.

    Note:
    - Pod will not be restarted, you must restart the pod for any pod-related changes to proliferate.

    Returns updated pod object.
    """
    logger.info(f"UPDATE /pods/{pod_id} - Top of update_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    
    pre_update_pod = pod.dict().copy()

    # Pod existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_pod.dict(exclude_unset=True)
    for key, value in input_data.items():
        setattr(pod, key, value)

    post_update_pod = pod.dict().copy()

    # Only update if there's a change
    if post_update_pod != pre_update_pod:
        updated_fields = {key: post_update_pod[key] for key in post_update_pod if key in pre_update_pod and post_update_pod[key] != pre_update_pod[key]}
        # Add updated field names to pod's modified_fields list
        current_modified_fields = set(pod.modified_fields or [])
        new_modified_fields = set(updated_fields.keys())
        pod.modified_fields = list(current_modified_fields.union(new_modified_fields))
        pod.db_update(f"'{g.username}' updated pod, updated_fields: {json.dumps(updated_fields)}")
    else:
        return error(result=pod.display(), msg="Incoming data made no changes to pod. Is incoming data equal to current data?")
        
    return ok(
        result=pod.display(),
        msg="Pod updated successfully.",
        metadata={"note":("Pod will require restart when updating command, environment_variables,",
                          "status_requested, volume_mounts, networking, or resources.")})


@router.delete(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="delete_pod",
    operation_id="delete_pod",
    response_model=PodDeleteResponse)
async def delete_pod(pod_id):
    """
    Delete a pod.

    Returns "".
    """
    logger.info(f"DELETE /pods/{pod_id} - Top of delete_pod.")

    # Needs to delete pod, service, db_pod, db_password
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    password = Password.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    pod.db_delete()
    password.db_delete()

    return ok(result="", msg="Pod successfully deleted.")


@router.get(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="get_pod",
    operation_id="get_pod",
    response_model=PodResponse)
async def get_pod(pod_id):
    """
    Get a pod.

    Returns retrieved pod object.
    """
    logger.info(f"GET /pods/{pod_id} - Top of get_pod.")

    # TODO .display(), search, permissions

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result=pod.display(), msg="Pod retrieved successfully.")


@router.get(
    "/pods/{pod_id}/derived",
    tags=["Pods"],
    summary="get_derived_pod",
    operation_id="get_derived_pod",
    response_model=PodResponse)
async def get_derived_pod(pod_id):
    """
    Derive a pod's final definition if templates are used.

    Returns final pod definition to be used for pod creation.
    """
    logger.info(f"GET /pods/{pod_id}/derived - Top of get_derived_pod.")

    input_pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod = PodBaseFull(**input_pod.dict().copy()) # Create a copy of pod data we'll merge template data into
    if pod.template:
        # Derive the final pod object by combining the pod and templates
        final_pod = combine_pod_and_template_recursively(pod, pod.template, tenant=g.request_tenant_id, site=g.site_id)
    else:
        final_pod = pod

    ###
    ### SECRETS
    ###
    # Need to replace all "<<TAPIS_vars>>" or "<<tapissecret_vars>>" with vals from secrets
    # currently just the passwords db table. Eventually that'll become pods_env which itself could reference sk if that's needed.
    pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
    pods_env = pods_env.dict()
    for key, val in final_pod.environment_variables.items():
        new_val = val
        if isinstance(val, str):
            # Find both TAPIS_ and tapissecret_ patterns
            tapis_matches = re.findall(r'<<TAPIS_(.*?)>>', val)
            tapissecret_matches = re.findall(r'<<tapissecret_(.*?)>>', val)
            
            # Handle TAPIS_ replacements
            for match in tapis_matches:
                new_val = new_val.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
            
            # Handle tapissecret_ replacements
            for match in tapissecret_matches:
                new_val = new_val.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
                
            final_pod.environment_variables[key] = new_val

    # Build metadata with template placeholder info if pod uses a template
    metadata = {}
    if pod.template:
        try:
            template_secret_map = get_template_merged_secret_map(
                pod.template,
                tenant=g.request_tenant_id,
                site=g.site_id
            )
            pod_secret_map = dict(final_pod.secret_map) if final_pod.secret_map else {}
            validation_result = validate_pod_secret_map_against_template(
                pod_secret_map,
                template_secret_map,
                actor=g.username
            )
            metadata = validation_result.metadata
        except Exception as e:
            logger.warning(f"Could not compute placeholder metadata for derived pod: {e}")

    return ok(result=final_pod.display(), metadata=metadata, msg="Final derived pod retrieved successfully.")

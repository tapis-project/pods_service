from fastapi import APIRouter
from channels import CommandChannel
from codes import REQUESTED, ON
from pydantic import ValidationError

from models_pods import Pod, NewPod, Password, PodsResponse, PodResponse, PodBase, PodBaseRead
from models_templates_utils import validate_pod_secret_map_against_template, get_template_merged_secret_map
from secret_utils import get_placeholder_warnings
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods

@router.get(
    "/pods",
    tags=["Pods"],
    summary="list_pods",
    operation_id="list_pods",
    response_model=PodsResponse)
async def list_pods():
    """
    Get all pods in your respective tenant and site that you have READ or higher access to.

    Returns a list of pods.
    """
    logger.info("GET /pods - Top of list_pods.")
    # TODO search
    pods =  Pod.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)
    pods_to_show = []
    metadata = {}
    final_msg = "Pods retrieved successfully."
    for pod in pods:
        try:
            # Validate using your response model (e.g., PodBase or whatever Pod.display() returns)
            pod_data = pod.display()
            PodBaseRead(**pod_data)  # This will raise if invalid
            pods_to_show.append(pod_data)
        except ValidationError as e:
            # Remove 'url' from each error dict because it's unsightly and not useful to end user
            error_list = e.errors()
            for err in error_list:
                err.pop('url', None)
            logger.warning(f"Pod {getattr(pod, 'pod_id', 'COULD NOT FIND PODID')} failed validation: {error_list}")
            if "warnings" not in metadata:
                metadata["warnings"] = []
            metadata["warnings"].append(
                f"Pod {getattr(pod, 'pod_id', None)} failed validation; omitting; reach out to admin; this debug might help: {error_list}"
            )
            final_msg = "Some pods failed validation. Please check metadata.warnings for details."
    logger.info("Pods retrieved.")
    return ok(result=pods_to_show, metadata=metadata, msg=final_msg)

@router.post(
    "/pods",
    tags=["Pods"],
    summary="create_pod",
    operation_id="create_pod",
    response_model=PodResponse)
async def create_pod(new_pod: NewPod):
    """
    Create a pod with inputted information.
    
    Notes:
    - Author will be given ADMIN level permissions to the pod.
    - status_requested defaults to "ON". So pod will immediately begin creation.

    Returns new pod object.
    """
    logger.info("POST /pods - Top of create_pod.")
    # Create full Pod object. Validates as well.
    pod = Pod(**new_pod.dict())
    
    # Create list of modified fields (which pertain to the user-changed PodBase fields).
    for arg in new_pod.dict(exclude_unset=True).keys():
        if arg not in PodBase.__fields__.keys():
            raise ValueError(f"modified_fields must match the fields of the Pod object. Got {arg}.")
        # resources is dict. Need to list if resources.cpu, etc are changed.
        # networking and volume_mounts are lists. Don't need to do anything extra.
        if arg == "resources":
            for sub_arg in new_pod.resources.dict(exclude_unset=True).keys():
                pod.modified_fields.append(f"resources.{sub_arg}")
        else:
            pod.modified_fields.append(arg)

    # Validate secret_map placeholders if pod uses a template
    placeholder_metadata = {}
    if pod.template:
        try:
            # Get merged secret_map from all chained templates
            template_secret_map = get_template_merged_secret_map(
                pod.template, 
                tenant=g.request_tenant_id, 
                site=g.site_id
            )
            
            # Validate that pod's secret_map overrides all required placeholders
            pod_secret_map = pod.secret_map or {}
            if hasattr(pod_secret_map, 'dict'):
                pod_secret_map = pod_secret_map.dict()
            
            validation_result = validate_pod_secret_map_against_template(
                pod_secret_map,
                template_secret_map,
                actor=getattr(g, 'username', None)
            )
            
            if not validation_result.is_valid:
                raise ValueError(validation_result.error_message)
            
            # Capture placeholder metadata from validation (already computed)
            placeholder_metadata = validation_result.metadata
                
        except Exception as e:
            logger.error(f"Error validating template placeholders for pod {pod.pod_id}: {e}")
            raise ValueError(f"{str(e)}")
    elif pod.secret_map:
        # No template - check pod's own secret_map for any unresolved placeholders
        warnings, _ = get_placeholder_warnings(pod.secret_map, actor=g.username)
        if warnings:
            # Build simple string list format
            required = [f"REQUIRED: '{w['env_var']}' - {w.get('description', 'No description')}. Override in secret_map."
                       for w in warnings if not w.get("has_default")]
            optional = [f"OPTIONAL: '{w['env_var']}' - {w.get('description', 'No description')}. Default: '{w.get('default_value')}'."
                       for w in warnings if w.get("has_default")]
            if required or optional:
                placeholder_metadata["available_placeholders"] = {
                    "required": required,
                    "optional": optional
                }

    # Create pod password db entry. If it's successful, we continue.
    password = Password(pod_id=pod.pod_id)
    password.db_create()
    logger.debug(f"Created password entry for {pod.pod_id}")
    # Create pod database entry
    pod.db_create()
    logger.debug(f"New pod saved in db. pod_id: {pod.pod_id}; image: {pod.image}; tenant: {g.request_tenant_id}.")
    # If status_requested = On, then we request pod and put a command. Else leave in default STOPPED state. 
    if pod.status_requested == ON:
        pod.status = REQUESTED
        pod.db_update()
        # Send command to start new pod
        ch = CommandChannel(name=pod.site_id)
        ch.put_cmd(object_id=pod.pod_id,
                   object_type="pod",
                   tenant_id=pod.tenant_id,
                   site_id=pod.site_id)
        ch.close()
        logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")
    
    return ok(result=pod.display(), metadata=placeholder_metadata, msg="Pod created successfully.")

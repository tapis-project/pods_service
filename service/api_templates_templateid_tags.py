from typing import Union
from fastapi import Query, Path, File, APIRouter
from models_misc import SetPermission
from models_templates import Template, TemplatePermissionsResponse
from models_templates_tags import TemplateTagsResponse, TemplateTagResponse, NewTemplateTag, TemplateTag, TemplateTagsSmallResponse, TemplateTagsWithDependentsResponse
from models_templates_utils import validate_template_tag_secret_map, validate_template_tag_env_vars
from models_volume_mounts_utils import (
    validate_template_volume_mounts,
    validate_template_volume_mounts_placeholders,
    get_volume_placeholder_warnings
)
from models_template_dependencies import (
    is_user_allowed_for_dependencies,
    get_template_dependencies
)
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/pods/templates/{template_id}/tags",
    tags=["Templates"],
    summary="list_template_tags",
    operation_id="list_template_tags",
    response_model=Union[TemplateTagsWithDependentsResponse, TemplateTagsResponse],
    response_model_exclude_none=True)
async def list_template_tags(
    template_id: str,
    full: bool = Query(True, description="Return pod_definition in tag when full=true"),
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    include_dependencies: bool = Query(False, description="Include dependency information (admin only). Shows which pods and tags depend on each template tag.")
):
    """
    List tag entries the template has

    Returns the ledger of template tags
    """
    logger.info(f"GET /pods/templates/{template_id}/tags - Top of list_template_tags with full={full}, include_dependencies={include_dependencies}.")
    template_tags = TemplateTag.db_get_where(where_params=[['template_id', '.eq', template_id]], sort_column='creation_ts', tenant="siteadmintable", site=g.site_id)

    # Check if user can view dependencies
    can_view_deps = include_dependencies #and is_user_allowed_for_dependencies(g.username, getattr(g, 'admin', False))
    logger.debug(f"can_view_deps={can_view_deps}")
    
    # Get dependencies if requested and allowed
    tag_deps_lookup = {}
    if can_view_deps:
        try:
            deps = get_template_dependencies(
                template_id=template_id,
                tenant="siteadmintable",
                site=g.site_id
            )
            logger.debug(f"Got {len(deps)} dependency records for template {template_id}")
            for dep in deps:
                tag_deps_lookup[dep['tag_timestamp']] = dep
        except Exception as e:
            logger.warning(f"Failed to fetch template dependencies: {e}")

    display_template_tags = []
    for template_tag in template_tags:
        if full:
            tag_display = template_tag.display(include_configs=include_configs)
        else:
            tag_display = template_tag.display_small()
        
        if can_view_deps:
            tag_dep = tag_deps_lookup.get(template_tag.tag_timestamp, {})
            tag_display['dependents'] = {
                'dependant_pods': tag_dep.get('dependant_pods', []),
                'dependant_pod_count': tag_dep.get('dependant_pod_count', 0),
                'dependant_tags': tag_dep.get('dependant_tags', []),
                'dependant_tags_count': tag_dep.get('dependant_tags_count', 0)
            }
        
        display_template_tags.append(tag_display)


    return ok(result=display_template_tags, msg="Template tags retrieved successfully.")


@router.post(
    "/pods/templates/{template_id}/tags",
    tags=["Templates"],
    summary="add_template_tag",
    operation_id="add_template_tag",
    response_model=TemplateTagResponse)
async def add_template_tag(template_id: str, new_template_tag: NewTemplateTag):
    """
    Add a new tag to a template.
    
    Template tags can include a ``pod_definition`` with ``secret_map`` to define placeholders 
    that users must or can optionally override when creating pods from this template.
    
    Secret Placeholders (25Q4 Feature):
    - ``${pods:default:value:?description}`` - Placeholder with a default value
    - ``${:?description}`` - Required placeholder (pod creation fails if not overridden)
    - ``${secret:name}`` - Direct secret reference (for templates with pre-configured secrets)
    
    When users create pods from this template, they can override placeholders with actual
    secret references in their pod's ``secret_map``.

    Returns new template tag object.
    """
    logger.info(f"POST /pods/templates/{template_id}/tags - Top of add_template.")
    
    metadata = {}
    pod_def = new_template_tag.pod_definition
    
    # Validate secret_map placeholders (templates must use placeholders, not direct secret refs)
    if pod_def and hasattr(pod_def, 'secret_map') and pod_def.secret_map:
        result = validate_template_tag_secret_map(pod_def.secret_map, actor=getattr(g, 'username', None))
        if not result.is_valid:
            raise ValueError(result.error_message)
        if result.metadata:
            metadata.update(result.metadata)
    
    # Validate environment_variables reference existing secret_map keys
    if pod_def and hasattr(pod_def, 'environment_variables') and pod_def.environment_variables:
        secret_map = pod_def.secret_map if hasattr(pod_def, 'secret_map') else {}
        result = validate_template_tag_env_vars(pod_def.environment_variables, secret_map)
        if not result.is_valid:
            raise ValueError(result.error_message)
    
    # Validate volume_mounts - templates must use placeholders for source_id, not literal volume IDs
    if pod_def and hasattr(pod_def, 'volume_mounts') and pod_def.volume_mounts:
        volume_mounts = pod_def.volume_mounts
        # Convert to dict if it's a Pydantic model with model_dump/dict method
        if hasattr(volume_mounts, 'model_dump'):
            volume_mounts = volume_mounts.model_dump()
        elif hasattr(volume_mounts, 'dict'):
            volume_mounts = volume_mounts.dict()
        
        # First validate that templates use placeholders, not literal volume IDs
        placeholder_valid, placeholder_errors = validate_template_volume_mounts_placeholders(volume_mounts)
        if not placeholder_valid:
            # Format error messages
            error_msgs = [f"Mount '{e['mount_path']}': {e['description']}" for e in placeholder_errors]
            raise ValueError(f"Template volume_mounts validation failed: {'; '.join(error_msgs)}")
        
        # Add placeholder info to metadata so users know what to override
        placeholder_warnings, _ = get_volume_placeholder_warnings(volume_mounts)
        if placeholder_warnings:
            metadata["volume_placeholders"] = {
                "required": [
                    {
                        "mount_path": w["mount_path"],
                        "mount_type": w["mount_type"],
                        "description": w["description"]
                    }
                    for w in placeholder_warnings
                ]
            }
        
        # Validate basic structure (ephemeral configs, etc.)
        result = validate_template_volume_mounts(
            volume_mounts=volume_mounts,
            user=g.username,
            tenant=g.request_tenant_id,
            site=g.site_id,
            roles=getattr(g, 'roles', None),
            is_template_creation=True  # Block on permission errors
        )
        if not result.is_valid:
            raise ValueError(result.error_message)
        if result.metadata:
            metadata.update(result.metadata)
    
    template_tag = TemplateTag(template_id=template_id, **new_template_tag.dict())

    # Create template database entry
    template_tag.db_create(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"New template_tag saved in db. template_id: {template_tag.template_id}; tenant: {g.request_tenant_id}.")

    return ok(result=template_tag.display(), msg="Template tag added successfully.", metadata=metadata)

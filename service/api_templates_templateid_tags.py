from typing import Union
from fastapi import Query, Path, File, APIRouter
from models_misc import SetPermission
from models_templates import Template, TemplatePermissionsResponse
from models_templates_tags import TemplateTagsResponse, TemplateTagResponse, NewTemplateTag, TemplateTag, TemplateTagsSmallResponse
from models_templates_utils import validate_template_tag_secret_map, validate_template_tag_env_vars
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
    response_model=TemplateTagsResponse)
async def list_template_tags(template_id: str, full: bool = Query(True, description="Return pod_definition in tag when full=true")):
    """
    List tag entries the template has

    Returns the ledger of template tags
    """
    logger.info(f"GET /pods/templates/{template_id}/tags - Top of list_template_tags with full={full}.")
    template_tags = TemplateTag.db_get_where(where_params=[['template_id', '.eq', template_id]], sort_column='creation_ts', tenant=g.request_tenant_id, site=g.site_id)

    display_template_tags = []
    for template_tag in template_tags:
        if full:
            display_template_tags.append(template_tag.display())
        else:
            display_template_tags.append(template_tag.display_small())


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
    - ``${default:value:?description}`` - Placeholder with a default value
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
    
    template_tag = TemplateTag(template_id=template_id, **new_template_tag.dict())

    # Create template database entry
    template_tag.db_create(tenant=g.request_tenant_id, site=g.site_id)
    logger.debug(f"New template_tag saved in db. template_id: {template_tag.template_id}; tenant: {g.request_tenant_id}.")

    return ok(result=template_tag.display(), msg="Template tag added successfully.", metadata=metadata)

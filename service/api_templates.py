from fastapi import Query, APIRouter
from models_templates import Template, TemplatesResponse, TemplateResponse, NewTemplate
from models_templates_tags import TemplateTag
from models_template_dependencies import (
    is_user_allowed_for_dependencies, 
    get_all_template_dependencies,
    get_template_dependencies
)
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok

from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)


router = APIRouter()


#### /pods/templates

@router.get(
    "/pods/templates",
    tags=["Templates"],
    summary="list_templates",
    operation_id="list_templates",
    response_model=TemplatesResponse)
async def list_templates(
    include_dependencies: bool = Query(False, description="Include dependency information (admin only). Shows which pods and tags depend on each template tag.")
):
    """
    Get all templates allowed globally + in respective tenant + for specific user.
    Returns a list of templates.
    """
    logger.info("GET /pods/templates - Top of list_templates.")

    # TODO search
    templates =  Template.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    # Check if user can view dependencies
    can_view_deps = include_dependencies and is_user_allowed_for_dependencies(g.username, getattr(g, 'admin', False))
    
    # Get all dependencies if requested and allowed
    dependencies_by_template = {}
    if can_view_deps:
        try:
            dependencies_by_template = get_all_template_dependencies(
                tenant="siteadmintable",
                site=g.site_id
            )
        except Exception as e:
            logger.warning(f"Failed to fetch template dependencies: {e}")

    templates_to_show = []
    for template in templates:
        template_display = template.display()
        if can_view_deps:
            template_display['tag_dependents'] = dependencies_by_template.get(template.template_id, [])
        templates_to_show.append(template_display)

    logger.info("Templates retrieved.")
    return ok(result=templates_to_show, msg="Templates retrieved successfully.")


@router.get(
    "/pods/templates/tags",
    tags=["Templates"],
    summary="list_templates_and_tags",
    operation_id="list_templates_and_tags",
    response_model=dict)
async def list_templates_and_tags(
    full: bool = Query(True, description="Returns tag pod_definition with tag when full=true"),
    include_dependencies: bool = Query(False, description="Include dependency information (admin only). Shows which pods and tags depend on each template tag.")
):
    """
    Get all templates and their tags for the user.
    Returns a dictionary with templates and their tags.
    """
    logger.info("GET /pods/templates/tags - Top of list_templates_and_tags.")

    # Fetch all templates
    templates = Template.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    list_of_templates = []
    for template in templates:
        list_of_templates.append(template.template_id)

    template_tags = TemplateTag.db_get_where(where_params=[['template_id', '.in', list_of_templates]], sort_column='creation_ts', tenant="siteadmintable", site=g.site_id)

    # Check if user can view dependencies
    can_view_deps = include_dependencies #and is_user_allowed_for_dependencies(g.username, getattr(g, 'admin', False))
    
    # Get all dependencies if requested and allowed
    dependencies_by_template = {}
    if can_view_deps:
        try:
            dependencies_by_template = get_all_template_dependencies(
                tenant="siteadmintable",
                site=g.site_id
            )
        except Exception as e:
            logger.warning(f"Failed to fetch template dependencies: {e}")

    templates_and_tags = {}
    for template in templates:
        template_id = template.template_id
        tags = []
        
        # Build a lookup for dependencies by tag_timestamp
        tag_deps_lookup = {}
        if can_view_deps and template_id in dependencies_by_template:
            for dep in dependencies_by_template[template_id]:
                tag_deps_lookup[dep['tag_timestamp']] = dep
        
        for tag in template_tags:
            if tag.template_id == template_id:
                tag_display = tag.display()
                if can_view_deps:
                    tag_dep = tag_deps_lookup.get(tag.tag_timestamp, {})
                    tag_display['dependents'] = {
                        'dependant_pods': tag_dep.get('dependant_pods', []),
                        'dependant_pod_count': tag_dep.get('dependant_pod_count', 0),
                        'dependant_tags': tag_dep.get('dependant_tags', []),
                        'dependant_tags_count': tag_dep.get('dependant_tags_count', 0)
                    }
                tags.append(tag_display)
        templates_and_tags[template_id] = {
            **template.display(),
            "tags": tags
        }
    logger.info("Templates and tags retrieved.")
    return ok(result=templates_and_tags, msg="Templates and tags retrieved successfully.")


@router.post(
    "/pods/templates",
    tags=["Templates"],
    summary="add_template",
    operation_id="add_template",
    response_model=TemplateResponse)
async def add_template(new_template: NewTemplate):
    """
    Add a template with inputted information.
    
    Returns new template object.
    """
    logger.info("POST /pods/templates - Top of add_template.")
    
    ### Validate input
    template = Template(**new_template.dict())

    # Create template database entry
    template.db_create(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"New template saved in db. template_id: {template.template_id}; tenant: {g.request_tenant_id}.")

    return ok(result=template.display(), msg="Template added successfully.")

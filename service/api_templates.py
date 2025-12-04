from fastapi import Query, APIRouter
from models_templates import Template, TemplatesResponse, TemplateResponse, NewTemplate
from models_templates_tags import TemplateTag
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
async def list_templates():
    """
    Get all templates allowed globally + in respective tenant + for specific user.
    Returns a list of templates.
    """
    logger.info("GET /pods/templates - Top of list_templates.")

    # TODO search
    templates =  Template.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    templates_to_show = []
    for template in templates:
        templates_to_show.append(template.display())

    logger.info("Templates retrieved.")
    return ok(result=templates_to_show, msg="Templates retrieved successfully.")


@router.get(
    "/pods/templates/tags",
    tags=["Templates"],
    summary="list_templates_and_tags",
    operation_id="list_templates_and_tags",
    response_model=dict)
async def list_templates_and_tags(full: bool = Query(True, description="Returns tag pod_definition with tag when full=true")):
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

    templates_and_tags = {}
    for template in templates:
        template_id = template.template_id
        tags = []
        for tag in template_tags:
            if tag.template_id == template_id:
                tags.append(tag.display())
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

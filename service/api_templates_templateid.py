from fastapi import APIRouter
from models_templates import Template, TemplateResponse, TemplateDeleteResponse, NewTemplate, UpdateTemplate
from models_templates_tags import TemplateTag
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()

# #### /pods/templates/{template_id}

@router.put(
    "/pods/templates/{template_id}",
    tags=["Templates"],
    summary="update_template",
    operation_id="update_template",
    response_model=TemplateResponse)
async def update_template(template_id, update_template: UpdateTemplate):
    """
    Update a template.

    Note:
    - Fields that change template id cannot be modified. Please recreate your template in that case.

    Returns updated template object.
    """
    logger.info(f"UPDATE /pods/template/{template_id} - Top of update_template.")

    template = Template.db_get_with_pk(template_id, tenant="siteadmintable", site=g.site_id)

    pre_update_template = template.copy()

    # Volume existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_template.dict(exclude_unset=True)

    if input_data.get("permissions"):
        for permission in input_data["permissions"]:
            user, level = permission.split(":")
            if user == "**" and not g.admin:
                raise HTTPException(
                    status_code=403,
                    detail="Only admins can set user='**' in template permissions."
                )

    for key, value in input_data.items():
        setattr(template, key, value)

    # Only update if there's a change
    if template != pre_update_template:
        template.db_update(tenant='siteadmintable', site=g.site_id)
    else:
        return error(result=template.display(), msg="Incoming data made no changes to template. Is incoming data equal to current data?")

    return ok(result=template.display(), msg="Template updated successfully.")
    


@router.delete(
    "/pods/templates/{template_id}",
    tags=["Templates"],
    summary="delete_template",
    operation_id="delete_template",
    response_model=TemplateDeleteResponse)
async def delete_template(template_id):
    """
    Delete a template.

    Returns "".
    """
    logger.info(f"DELETE /pods/templates/{template_id} - Top of delete_template.")

    # must happen before Template.db_delete()
    # delete all TemplateTags associated with this template
    TemplateTags = TemplateTag.db_get_where(where_params=[['template_id', '.eq', template_id]], sort_column="creation_ts", tenant="siteadmintable", site=g.site_id)
    logger.debug(f"depleting TemplateTags: {TemplateTags}")
    for template_tag in TemplateTags:
        logger.debug(f"Deleting template_tag: {template_tag}")
        template_tag.db_delete(tenant="siteadmintable", site=g.site_id)

    template = Template.db_get_with_pk(template_id, tenant="siteadmintable", site=g.site_id)
    template.db_delete(tenant="siteadmintable", site=g.site_id)


    return ok(result="", msg="Template and associated Template Tags successfully deleted.")


@router.get(
    "/pods/templates/{template_id}",
    tags=["Templates"],
    summary="get_template",
    operation_id="get_template",
    response_model=TemplateResponse)
async def get_template(template_id):
    """
    Get a template.

    Returns retrieved templates object.
    """
    logger.info(f"GET /pods/templates/{template_id} - Top of get_template.")

    # TODO search
    template = Template.db_get_with_pk(template_id, tenant="siteadmintable", site=g.site_id)

    return ok(result=template.display(), msg="Template retrieved successfully.")

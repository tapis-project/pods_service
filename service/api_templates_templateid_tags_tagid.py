from typing import Union
from fastapi import Query, Path, File, APIRouter
from models_misc import SetPermission
from models_templates import Template, TemplatePermissionsResponse
from models_templates_tags import TemplateTagsResponse, TemplateTagResponse, NewTemplateTag, TemplateTag, TemplateTagsSmallResponse
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/pods/templates/{template_id}/tags/{tag_id}",
    tags=["Templates"],
    summary="get_template_tag",
    operation_id="get_template_tag",
    response_model=TemplateTagsResponse)
async def get_template_tag(template_id: str, tag_id: str):
    """
    Get a specific tag entry the template has

    Returns the tag entry
    """
    logger.info(f"GET /pods/templates/{template_id}/tags/{tag_id} - Top of get_template_tag.")
    where_params = [['template_id', '.eq', template_id]]
    if "@" in tag_id: ### You could use just periods too if you need an alternative #or "." in tag_id:
        where_params.append(['tag_timestamp', '.eq', tag_id])
    else:
        where_params.append(['tag', '.eq', tag_id])
    template_tags = TemplateTag.db_get_where(where_params=where_params, sort_column="creation_ts", tenant=g.request_tenant_id, site=g.site_id)

    display_template_tags = []
    for template_tag in template_tags:
        display_template_tags.append(template_tag.display())

    return ok(result=display_template_tags, msg = "Template tags retrieved and filtered successfully.")


@router.delete(
    "/pods/templates/{template_id}/tags/{tag_id}",
    tags=["Templates"],
    summary="delete_template_tag",
    operation_id="delete_template_tag",
    response_model=TemplateTagResponse)
async def delete_template_tag(template_id: str, tag_id: str):
    pass
from typing import Union
from fastapi import Query, Path, File, APIRouter
from models_misc import SetPermission
from models_templates import Template, TemplatePermissionsResponse
from models_templates_tags import TemplateTagsResponse, TemplateTagResponse, TemplateTagDeleteResponse, NewTemplateTag, TemplateTag, TemplateTagsSmallResponse, TemplateTagsWithDependentsResponse
from models_template_dependencies import (
    is_user_allowed_for_dependencies,
    get_tag_dependencies
)
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.config import conf
from tapisservice.logs import get_logger
from errors import ResourceError
logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/pods/templates/{template_id}/tags/{tag_id}",
    tags=["Templates"],
    summary="get_template_tag",
    operation_id="get_template_tag",
    response_model=Union[TemplateTagsWithDependentsResponse, TemplateTagsResponse],
    response_model_exclude_none=True)
async def get_template_tag(
    template_id: str,
    tag_id: str,
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    include_dependencies: bool = Query(False, description="Include dependency information (admin only). Shows which pods and tags depend on this template tag.")
):
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
    template_tags = TemplateTag.db_get_where(where_params=where_params, sort_column="creation_ts", tenant="siteadmintable", site=g.site_id)

    # Check if user can view dependencies
    can_view_deps = include_dependencies #and is_user_allowed_for_dependencies(g.username, getattr(g, 'admin', False))
    
    # Get dependencies if requested and allowed
    tag_deps_lookup = {}
    if can_view_deps:
        try:
            deps = get_tag_dependencies(
                template_id=template_id,
                tag_timestamp=tag_id,
                tenant="siteadmintable",
                site=g.site_id
            )
            if deps:
                for dep in deps:
                    tag_deps_lookup[dep['tag_timestamp']] = dep
        except Exception as e:
            logger.warning(f"Failed to fetch tag dependencies: {e}")

    display_template_tags = []
    for template_tag in template_tags:
        tag_display = template_tag.display(include_configs=include_configs)
        
        if can_view_deps:
            tag_dep = tag_deps_lookup.get(template_tag.tag_timestamp, {})
            tag_display['dependents'] = {
                'dependant_pods': tag_dep.get('dependant_pods', []),
                'dependant_pod_count': tag_dep.get('dependant_pod_count', 0),
                'dependant_tags': tag_dep.get('dependant_tags', []),
                'dependant_tags_count': tag_dep.get('dependant_tags_count', 0)
            }
        
        display_template_tags.append(tag_display)

    return ok(result=display_template_tags, msg = "Template tags retrieved and filtered successfully.")


@router.delete(
    "/pods/templates/{template_id}/tags/{tag_id}",
    tags=["Templates"],
    summary="delete_template_tag",
    operation_id="delete_template_tag",
    response_model=TemplateTagDeleteResponse)
async def delete_template_tag(
    template_id: str,
    tag_id: str,
    force: bool = Query(False, description="Force deletion even if pods or other template tags depend on this tag. Use with caution.")
):
    """
    Delete a specific template tag. (Admin only)
    
    If the tag has dependent pods or other template tags that inherit from it,
    deletion will fail unless the `force=true` query parameter is provided.
    
    Returns the deleted tag information.
    """
    logger.info(f"DELETE /pods/templates/{template_id}/tags/{tag_id} - Top of delete_template_tag.")
    
    # Check authorization - only admins or allowed users can delete template tags
    if not is_user_allowed_for_dependencies(g.username, getattr(g, 'admin', False)):
        return error(
            result=None, 
            msg="Not authorized. Only admins can delete template tags."
        )
    
    # Find the template tag(s) to delete
    where_params = [['template_id', '.eq', template_id]]
    if "@" in tag_id:
        where_params.append(['tag_timestamp', '.eq', tag_id])
    else:
        where_params.append(['tag', '.eq', tag_id])
    
    template_tags = TemplateTag.db_get_where(
        where_params=where_params, 
        sort_column="creation_ts", 
        tenant="siteadmintable", 
        site=g.site_id
    )
    
    if not template_tags:
        raise ResourceError(f"Template tag '{tag_id}' not found for template '{template_id}'.", 404)
    
    # For safety, only delete one tag at a time when using tag name (not full timestamp)
    # If multiple tags match (same tag name, different timestamps), require full tag_id
    if len(template_tags) > 1 and "@" not in tag_id:
        return error(
            result=None, 
            msg=f"Multiple tags match '{tag_id}'. Please specify full tag_timestamp (tag@timestamp) to delete a specific version."
        )
    
    template_tag = template_tags[0]
    
    # Check for dependencies
    try:
        deps = get_tag_dependencies(
            template_id=template_id,
            tag_timestamp=template_tag.tag_timestamp,
            tenant="siteadmintable",
            site=g.site_id
        )
    except Exception as e:
        logger.warning(f"Failed to check tag dependencies: {e}")
        deps = None
    
    # Get dependency counts
    dependant_pod_count = 0
    dependant_tags_count = 0
    
    if deps:
        for dep in deps:
            if dep.get('tag_timestamp') == template_tag.tag_timestamp:
                dependant_pod_count = dep.get('dependant_pod_count', 0)
                dependant_tags_count = dep.get('dependant_tags_count', 0)
                break
    
    has_dependencies = dependant_pod_count > 0 or dependant_tags_count > 0
    
    # If there are dependencies and force is not set, return error with counts
    if has_dependencies and not force:
        msg_parts = []
        if dependant_pod_count > 0:
            msg_parts.append(f"{dependant_pod_count} pod(s)")
        if dependant_tags_count > 0:
            msg_parts.append(f"{dependant_tags_count} template tag(s)")
        
        dependency_msg = " and ".join(msg_parts)
        return error(
            result={
                "template_id": template_id,
                "tag": template_tag.tag,
                "tag_timestamp": template_tag.tag_timestamp,
                "dependant_pod_count": dependant_pod_count,
                "dependant_tags_count": dependant_tags_count
            },
            msg=f"Cannot delete template tag '{template_tag.tag_timestamp}': {dependency_msg} depend on it. Use force=true to delete anyway."
        )
    
    # Perform the deletion
    tag_display = template_tag.display()
    template_tag.db_delete(tenant="siteadmintable", site=g.site_id)
    
    # Build appropriate success message
    if has_dependencies and force:
        msg = f"Template tag '{template_tag.tag_timestamp}' force deleted. Warning: {dependant_pod_count} pod(s) and {dependant_tags_count} template tag(s) were depending on it."
    else:
        msg = f"Template tag '{template_tag.tag_timestamp}' deleted successfully."
    
    logger.info(msg)
    return ok(result=tag_display, msg=msg)
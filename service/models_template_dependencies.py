"""
Helper functions for querying the template_dependencies materialized view.

The materialized view contains:
- template_id: The template ID
- tag_timestamp: The tag timestamp
- full_tag: template_id:tag_timestamp
- dependant_pods: Array of pod_ids using this template tag
- dependant_pod_count: Count of pods using this template tag  
- dependant_tags: Array of template tags that inherit from this tag
- dependant_tags_count: Count of dependent template tags

Access is restricted to admins and specific users (e.g., 'cgarcia').
"""

from typing import Dict, List, Optional, Any
from sqlalchemy import text
from stores import pg_store
from tapisservice.logs import get_logger

logger = get_logger(__name__)


# Users allowed to access template dependencies (besides admins)
ALLOWED_DEPENDENCY_USERS = ['cgarcia']


def is_user_allowed_for_dependencies(username: str, is_admin: bool) -> bool:
    """
    Check if user is allowed to view template dependencies.
    
    Args:
        username: The username to check
        is_admin: Whether the user has admin role
        
    Returns:
        True if user is allowed, False otherwise
    """
    return is_admin or username in ALLOWED_DEPENDENCY_USERS


def get_template_dependencies(
    template_id: str,
    tenant: str,
    site: str
) -> List[Dict[str, Any]]:
    """
    Get dependency information for all tags of a given template.
    
    Args:
        template_id: The template ID to query
        tenant: Tenant for database access (use 'siteadmintable' for templates)
        site: Site for database access
        
    Returns:
        List of dependency records for all tags of the template
    """
    logger.debug(f"Getting template dependencies for template_id: {template_id}")
    
    store = pg_store[site][tenant]
    
    query = text("""
        SELECT 
            template_id,
            tag_timestamp,
            full_tag,
            dependant_pods,
            dependant_pod_count,
            dependant_tags,
            dependant_tags_count
        FROM template_dependencies
        WHERE template_id = :template_id
        ORDER BY tag_timestamp DESC
    """)
    
    result = store.run("execute", query, fn_params={"parameters": {"template_id": template_id}}, all=True)
    
    dependencies = []
    for row in result:
        dependencies.append({
            "template_id": row.template_id,
            "tag_timestamp": row.tag_timestamp,
            "full_tag": row.full_tag,
            "dependant_pods": row.dependant_pods or [],
            "dependant_pod_count": row.dependant_pod_count or 0,
            "dependant_tags": row.dependant_tags or [],
            "dependant_tags_count": row.dependant_tags_count or 0
        })
    
    return dependencies


def get_tag_dependencies(
    template_id: str,
    tag_timestamp: str,
    tenant: str,
    site: str
) -> Optional[Dict[str, Any]]:
    """
    Get dependency information for a specific template tag.
    
    Args:
        template_id: The template ID
        tag_timestamp: The tag timestamp (tag@timestamp or just tag)
        tenant: Tenant for database access
        site: Site for database access
        
    Returns:
        Dependency record for the tag, or None if not found
    """
    logger.debug(f"Getting tag dependencies for {template_id}:{tag_timestamp}")
    
    store = pg_store[site][tenant]
    
    # Handle both tag@timestamp format and just tag
    if "@" in tag_timestamp:
        # Full tag_timestamp provided
        query = text("""
            SELECT 
                template_id,
                tag_timestamp,
                full_tag,
                dependant_pods,
                dependant_pod_count,
                dependant_tags,
                dependant_tags_count
            FROM template_dependencies
            WHERE template_id = :template_id AND tag_timestamp = :tag_timestamp
        """)
        params = {"template_id": template_id, "tag_timestamp": tag_timestamp}
    else:
        # Just tag name, match by prefix
        query = text("""
            SELECT 
                template_id,
                tag_timestamp,
                full_tag,
                dependant_pods,
                dependant_pod_count,
                dependant_tags,
                dependant_tags_count
            FROM template_dependencies
            WHERE template_id = :template_id AND tag_timestamp LIKE :tag_pattern
            ORDER BY tag_timestamp DESC
        """)
        params = {"template_id": template_id, "tag_pattern": f"{tag_timestamp}@%"}
    
    result = store.run("execute", query, fn_params={"parameters": params}, all=True)
    
    dependencies = []
    for row in result:
        dependencies.append({
            "template_id": row.template_id,
            "tag_timestamp": row.tag_timestamp,
            "full_tag": row.full_tag,
            "dependant_pods": row.dependant_pods or [],
            "dependant_pod_count": row.dependant_pod_count or 0,
            "dependant_tags": row.dependant_tags or [],
            "dependant_tags_count": row.dependant_tags_count or 0
        })
    
    return dependencies if dependencies else None


def get_all_template_dependencies(
    tenant: str,
    site: str
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get dependency information for all templates, grouped by template_id.
    
    Args:
        tenant: Tenant for database access
        site: Site for database access
        
    Returns:
        Dict mapping template_id to list of tag dependency records
    """
    logger.debug("Getting all template dependencies")
    
    store = pg_store[site][tenant]
    
    query = text("""
        SELECT 
            template_id,
            tag_timestamp,
            full_tag,
            dependant_pods,
            dependant_pod_count,
            dependant_tags,
            dependant_tags_count
        FROM template_dependencies
        ORDER BY template_id, tag_timestamp DESC
    """)
    
    result = store.run("execute", query, all=True)
    
    dependencies_by_template = {}
    for row in result:
        template_id = row.template_id
        if template_id not in dependencies_by_template:
            dependencies_by_template[template_id] = []
        
        dependencies_by_template[template_id].append({
            "tag_timestamp": row.tag_timestamp,
            "full_tag": row.full_tag,
            "dependant_pods": row.dependant_pods or [],
            "dependant_pod_count": row.dependant_pod_count or 0,
            "dependant_tags": row.dependant_tags or [],
            "dependant_tags_count": row.dependant_tags_count or 0
        })
    
    return dependencies_by_template

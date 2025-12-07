from datetime import datetime
from typing import Dict, Any, Optional

from tapisservice.tapisfastapi.utils import g
from tapisservice.logs import get_logger

logger = get_logger(__name__)


def log_secret_event(
    event_type: str,
    secret_id: str,
    sk_secret_name: str = None,
    actor: str = None,
    pod_id: str = None,
    tenant_id: str = None,
    site_id: str = None,
    details: Optional[Dict[str, Any]] = None
) -> None:
    """
    Log a secret-related event for auditing purposes.
    
    Args:
        event_type: Type of event (e.g., SECRET_CREATED, SECRET_ACCESSED, SECRET_VALIDATION_FAILED)
        secret_id: Name of the secret involved
        sk_secret_name: Full namespaced secret name used in Security Kernel
        actor: Username of the actor performing the action
        pod_id: Pod ID if the event is related to a pod
        tenant_id: Tenant ID for the event
        site_id: Site ID for the event
        details: Additional details about the event
    """
    actor = actor or getattr(g, 'username', 'unknown')
    tenant_id = tenant_id or getattr(g, 'request_tenant_id', 'unknown')
    site_id = site_id or getattr(g, 'site_id', 'unknown')
    
    log_entry = {
        "event_type": event_type,
        "secret_id": secret_id,
        "sk_secret_name": sk_secret_name,
        "actor": actor,
        "pod_id": pod_id,
        "tenant_id": tenant_id,
        "site_id": site_id,
        "timestamp": datetime.utcnow().isoformat(),
        "details": details or {}
    }
    
    # Log at appropriate level based on event type
    if "FAILED" in event_type or "ERROR" in event_type:
        logger.warning(f"Secret event: {event_type} - {log_entry}")
    else:
        logger.info(f"Secret event: {event_type} - {log_entry}")

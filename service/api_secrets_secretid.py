from fastapi import APIRouter
from models_secrets import Secret, SecretResponse, SecretDeleteResponse, UpdateSecret, SecretValueResponse, PODS_SERVICE_ACCOUNT, PODS_SERVICE_TENANT
from models_secret_logs import log_secret_event
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
from __init__ import t
from utils import check_permissions
from errors import PermissionsException, ResourceError
import codes

logger = get_logger(__name__)

router = APIRouter()


#### /pods/secrets/{secret_id}

@router.get(
    "/pods/secrets/{secret_id}",
    tags=["Secrets"],
    summary="get_secret",
    operation_id="get_secret",
    response_model=SecretResponse)
async def get_secret(secret_id: str):
    """
    Get secret metadata (not the secret value itself).
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Note:
    - Returns metadata only. Use GET /secrets/{name}/value to retrieve the actual value.
    - Requires READ permission on the secret.

    Returns secret metadata.
    """
    logger.info(f"GET /pods/secrets/{secret_id} - Top of get_secret.")

    secret = Secret.db_get_with_pk(secret_id, tenant="siteadmintable", site=g.site_id)
    if not secret:
        raise ResourceError(f"Secret with name '{secret_id}' not found.", 404)
    
    # Check if user has READ permission
    if not check_permissions(user=g.username, object=secret, object_type="secret", level=codes.READ, roles=g.roles):
        raise PermissionsException(f"You do not have permission to access secret '{secret_id}'.", 403)

    return ok(result=secret.display(), msg="Secret metadata retrieved successfully.")


@router.get(
    "/pods/secrets/{secret_id}/value",
    tags=["Secrets"],
    summary="get_secret_value",
    operation_id="get_secret_value",
    response_model=SecretValueResponse)
async def get_secret_value(secret_id: str):
    """
    Get the actual secret value from SK.
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Note:
    - Requires USER permission on the secret (higher than READ).
    - Secrets with ``readable=False`` cannot have their values retrieved via this endpoint.
    - Pod injection via ``secret_map`` always works regardless of ``readable`` setting.
    - This operation is logged.

    Returns the secret value.
    """
    logger.info(f"GET /pods/secrets/{secret_id}/value - Top of get_secret_value.")

    secret = Secret.db_get_with_pk(secret_id, tenant="siteadmintable", site=g.site_id)
    if not secret:
        raise ResourceError(f"Secret with name '{secret_id}' not found.", 404)
    
    # Check if user has USER permission (required to read secret values)
    if not check_permissions(user=g.username, object=secret, object_type="secret", level=codes.USER, roles=g.roles):
        raise PermissionsException(f"You do not have permission to access the value of secret '{secret_id}'.", 403)

    # Check if secret is readable
    if not secret.readable:
        # Log the denial
        log_secret_event(
            event_type="SECRET_READ_DENIED",
            secret_id=secret.secret_id,
            sk_secret_name=secret.sk_secret_name,
            actor=g.username,
            tenant_id=g.request_tenant_id,
            site_id=g.site_id,
            details={"reason": "readable=False", "readable": secret.readable, "writable": secret.writable}
        )
        raise PermissionsException(
            f"Secret '{secret_id}' has readable=False and its value cannot be retrieved via API. "
            f"This secret can still be used in pods via secret_map for environment variable injection. "
            f"To make the secret readable, delete and recreate with readable=True.", 403)

    try:
        # Read the secret from SK using Pods service account (version=0 means latest)
        logger.debug(f"Reading secret from SK with name: {secret.sk_secret_name} (latest version)")
        result = t.sk.readSecret(
            secretType='user',
            secretName=secret.sk_secret_name,
            version=0,
            tenant=PODS_SERVICE_TENANT,
            user=PODS_SERVICE_ACCOUNT,
            _tapis_set_x_headers_from_service=True
        )
        secret_value = result.secretMap.get('secret_value', '')
        logger.debug(f"Secret value retrieved from SK successfully.")
        
        # Log the read event
        log_secret_event(
            event_type="SECRET_READ",
            secret_id=secret.secret_id,
            sk_secret_name=secret.sk_secret_name,
            actor=g.username,
            tenant_id=g.request_tenant_id,
            site_id=g.site_id
        )
    except Exception as e:
        logger.error(f"Failed to read secret from SK: {e}")
        raise ResourceError(f"Failed to read secret from Security Kernel: {str(e)}", 500)

    return ok(result={"secret_value": secret_value}, msg="Secret value retrieved successfully.")


@router.put(
    "/pods/secrets/{secret_id}",
    tags=["Secrets"],
    summary="update_secret",
    operation_id="update_secret",
    response_model=SecretResponse)
async def update_secret(secret_id: str, update_secret: UpdateSecret):
    """
    Update a secret's description and/or value.
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Note:
    - Requires USER permission on the secret.
    - Secrets with ``writable=False`` cannot have their values updated (write-once secrets).
    - Description updates are always allowed regardless of ``writable`` setting.
    - Updates are logged.
    - Pods using this secret will get the new value on next start/restart.

    Returns updated secret metadata.
    """
    logger.info(f"PUT /pods/secrets/{secret_id} - Top of update_secret.")

    secret = Secret.db_get_with_pk(secret_id, tenant="siteadmintable", site=g.site_id)
    if not secret:
        raise ResourceError(f"Secret with name '{secret_id}' not found.", 404)
    
    # Check if user has USER permission
    if not check_permissions(user=g.username, object=secret, object_type="secret", level=codes.USER, roles=g.roles):
        raise PermissionsException(f"You do not have permission to update secret '{secret_id}'.", 403)

    # Update secret value in SK if provided
    if update_secret.secret_value is not None:
        # Check if secret is writable
        if not secret.writable:
            # Log the denial
            log_secret_event(
                event_type="SECRET_WRITE_DENIED",
                secret_id=secret.secret_id,
                sk_secret_name=secret.sk_secret_name,
                actor=g.username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id,
                details={"reason": "writable=False", "method": "PUT", "readable": secret.readable, "writable": secret.writable}
            )
            raise PermissionsException(
                f"Secret '{secret_id}' has writable=False and cannot have its value updated. "
                f"This secret is write-once and its value is permanently set. "
                f"Description updates are still allowed. "
                f"To create an updatable secret, delete this one and recreate with writable=True.",
                403)

        
        try:
            logger.debug(f"Updating secret value in SK with name: {secret.sk_secret_name}")
            t.sk.writeSecret(
                secretType='user',
                secretName=secret.sk_secret_name,
                tenant=PODS_SERVICE_TENANT,
                user=PODS_SERVICE_ACCOUNT,
                data={'secret_value': update_secret.secret_value},
                _tapis_set_x_headers_from_service=True
            )
            logger.debug(f"Secret value updated in SK successfully.")
            
            # Log the update event
            log_secret_event(
                event_type="SECRET_UPDATED",
                secret_id=secret.secret_id,
                sk_secret_name=secret.sk_secret_name,
                actor=g.username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id,
                details={"updated_value": True, "readable": secret.readable, "writable": secret.writable}
            )
        except Exception as e:
            logger.error(f"Failed to update secret in SK: {e}")
            raise PermissionsException(f"Failed to update secret in Security Kernel: {str(e)}", 500)

    # Update description in DB if provided
    if update_secret.description is not None:
        secret.description = update_secret.description
        secret.db_update(tenant="siteadmintable", site=g.site_id)

    return ok(result=secret.display(), msg="Secret updated successfully.")


@router.delete(
    "/pods/secrets/{secret_id}",
    tags=["Secrets"],
    summary="delete_secret",
    operation_id="delete_secret",
    response_model=SecretDeleteResponse)
async def delete_secret(secret_id: str):
    """
    Delete a secret from both the database and Security Kernel (SK).
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Note:
    - Requires ADMIN permission on the secret (only the creator has this by default).
    - This operation is permanent and cannot be undone.
    - Pods currently using this secret will fail to start until the secret reference is removed or a new secret with the same name is created by the pod owner.
    - All delete operations are logged.

    Returns the deleted secret name.
    """
    logger.info(f"DELETE /pods/secrets/{secret_id} - Top of delete_secret.")

    secret = Secret.db_get_with_pk(secret_id, tenant="siteadmintable", site=g.site_id)
    if not secret:
        raise ResourceError(f"Secret with name '{secret_id}' not found.", 404)
    
    # Check if user has ADMIN permission
    if not check_permissions(user=g.username, object=secret, object_type="secret", level=codes.ADMIN, roles=g.roles):
        raise PermissionsException(f"You do not have permission to delete secret '{secret_id}'.", 403)

    try:
        # Delete from SK using Pods service account
        # Note: destroySecret requires key parameter (unlike readSecret which has version for latest)
        logger.debug(f"Deleting secret from SK with name: {secret.sk_secret_name}")
        t.sk.destroySecretMeta(
            secretType='user',
            secretName=secret.sk_secret_name,
            tenant=PODS_SERVICE_TENANT,
            user=PODS_SERVICE_ACCOUNT,
            _tapis_set_x_headers_from_service=True
        )
        logger.debug(f"Secret deleted from SK successfully.")
        
        # Log the delete event
        log_secret_event(
            event_type="SECRET_DELETED",
            secret_id=secret.secret_id,
            sk_secret_name=secret.sk_secret_name,
            actor=g.username,
            tenant_id=g.request_tenant_id,
            site_id=g.site_id
        )
    except Exception as e:
        logger.error(f"Failed to delete secret from SK: {e}")

        raise ResourceError(f"Failed to delete secret from Security Kernel: {str(e)}", 500)

    # Delete from DB
    secret.db_delete(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"Secret deleted from DB.")

    return ok(result=secret_id, msg="Secret deleted successfully.")
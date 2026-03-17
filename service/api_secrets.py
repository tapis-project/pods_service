from datetime import datetime
from fastapi import APIRouter
from models_secrets import Secret, NewSecret, SecretsResponse, SecretResponse, SecretDeleteResponse, SecretValueResponse, PODS_SERVICE_ACCOUNT, PODS_SERVICE_TENANT
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


#### /pods/secrets

@router.get(
    "/pods/secrets",
    tags=["Secrets"],
    summary="list_secrets",
    operation_id="list_secrets",
    response_model=SecretsResponse)
async def list_secrets():
    """
    Get all secrets you have permission to access.
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Note:
    - Returns metadata only, not secret values (use GET /secrets/{name}/value).
    - Filter shows only secrets where you have READ+ permission.

    Returns a list of secrets (without secret values).
    """
    logger.info("GET /pods/secrets - Top of list_secrets.")

    # Get all secrets for this site
    secrets = Secret.db_get_all(tenant="siteadmintable", site=g.site_id)
    
    metadata = {}
    if getattr(g, 'admin_active', False):
        # Admin mode: show all secrets, check user's own access in-memory
        read_levels = {'READ', 'USER', 'ADMIN', 'APPROVEDADMIN'}
        user_secrets_ids = set()
        for secret in secrets:
            for perm in secret.permissions:
                user, level = perm.split(':', 1)
                if user == g.username and level in read_levels:
                    user_secrets_ids.add(secret.secret_id)
                    break
        secrets_to_show = [secret.display() for secret in secrets]
        admin_only_count = len(secrets_to_show) - len(user_secrets_ids)
        metadata["admin_context"] = {
            "admin_mode": True,
            "user_accessible_ids": list(user_secrets_ids),
            "msg": f"You can access {len(user_secrets_ids)} secrets, admin reveals {admin_only_count}"
        }
    else:
        # Normal mode: filter to only show secrets the user has READ+ permission to
        user_secrets = []
        for secret in secrets:
            if check_permissions(user=g.username, object=secret, object_type="secret", level=codes.READ, roles=g.roles):
                user_secrets.append(secret)
        secrets_to_show = [secret.display() for secret in user_secrets]

    logger.info(f"Secrets retrieved. Count: {len(secrets_to_show)}")
    return ok(result=secrets_to_show, metadata=metadata, msg="Secrets retrieved successfully.")


@router.post(
    "/pods/secrets",
    tags=["Secrets"],
    summary="create_secret",
    operation_id="create_secret",
    response_model=SecretResponse)
async def create_secret(new_secret: NewSecret):
    """
    Create a secret with inputted information.
    
    25Q4 Feature: Pods Secrets allow secure injection of credentials into pods.
    
    Notes:
    - This endpoint creates new secrets only. Returns 409 Conflict if secret_id already exists.
    - To update an existing secret, use PUT /pods/secrets/{secret_id}.
    - Secrets are stored securely in Tapis Security Kernel (SK).
    - Secret names are automatically namespaced: ``pods_{tenant}_user_{username}_{name}``
    - Only initial ADMIN can use this secret in pods unless you grant permissions.
    - Use ``secret_map`` in pod definitions to inject as environment variables.
    - All secret operations are logged

    Request Body Fields:
    - **secret_id** (required): Alphanumeric name with underscores/dashes allowed (max 100 chars)
    - **secret_value** (required): The actual secret value to store securely
    - **description** (optional): ASCII description (max 500 chars)
    - **scope** (optional): ``user`` (default) or ``pod`` - determines secret visibility
    - **pod_id** (optional): Required if scope is ``pod``, must be omitted if scope is ``user``
    - **readable** (optional): ``true`` (default) or ``false`` - controls if value can be retrieved via API
    - **writable** (optional): ``true`` (default) or ``false`` - controls if value can be updated via PUT
    
    Access Mode Combinations:
    - ``readable=true, writable=true`` (default): Full access - value can be read and updated
    - ``readable=true, writable=false``: Read-only - value can be read but not updated (write-once)
    - ``readable=false, writable=true``: Write-only - value can be updated but not read via API
    - ``readable=false, writable=false``: Locked - value cannot be read or updated, only used via pod injection
    
    Note: Pod injection via ``secret_map`` always works regardless of ``readable`` setting.

    Usage in Pods:
    After creating a secret, reference it in your pod's ``secret_map``; after that, reference it with ${} in the fields that support secrets::
    
        {
            "secret_map": {
                "DB_PASSWORD": "${secret:my_db_secret}"
            },
            "environment_variables": {
                "DATABASE_URL": "postgres://user:${pods:secrets:DB_PASSWORD}@localhost/db"
            }
        }

    Returns new secret object (without the secret value).
    """
    logger.info("POST /pods/secrets - Top of create_secret.")
    # Create secret object (validates as well)
    secret = Secret(**new_secret.dict(exclude={'secret_value'}))

    # Check if secret already exists - POST is create-only, return 409 if exists
    existing_secret = None
    try:
        existing_secret = Secret.db_get_with_pk(new_secret.secret_id, tenant="siteadmintable", site=g.site_id)
    except Exception as e:
        logger.critical(f"Error checking existing secret: {e}")

    if existing_secret:
        # Secret already exists - return 409 Conflict
        # Use PUT /pods/secrets/{secret_id} to update an existing secret
        raise ResourceError(
            f"Secret '{new_secret.secret_id}' already exists. "
            f"Use PUT /pods/secrets/{new_secret.secret_id} to update the secret value or description. "
            f"Use DELETE /pods/secrets/{new_secret.secret_id} first if you want to recreate it."
        , 409)

    # New secret - store in SK
    try:
        # Store the secret in SK under Pods service account
        # All secrets stored under service account, partitioned by sk_secret_name
        logger.debug(f"Storing secret in SK with name: {secret.sk_secret_name}")
        t.sk.writeSecret(
            secretType='user',
            secretName=secret.sk_secret_name,
            tenant=PODS_SERVICE_TENANT,
            user=PODS_SERVICE_ACCOUNT,
            data={'secret_value': new_secret.secret_value},
            _tapis_set_x_headers_from_service=True
        )
        logger.debug(f"Secret stored in SK successfully.")
    except Exception as e:
        logger.error(f"Failed to store secret in SK: {e}")
        raise ResourceError(f"Failed to store secret in Security Kernel: {str(e)}", 500)

    # Create secret database entry
    secret.db_create(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"New secret saved in db. secret_id: {secret.secret_id}; sk_secret_name: {secret.sk_secret_name}")

    # Log the creation event
    log_secret_event(
        event_type="SECRET_CREATED",
        secret_id=secret.secret_id,
        sk_secret_name=secret.sk_secret_name,
        actor=g.username,
        tenant_id=g.request_tenant_id,
        site_id=g.site_id,
        details={"scope": secret.scope, "pod_id": secret.pod_id, "readable": secret.readable, "writable": secret.writable}
    )

    return ok(result=secret.display(), msg="Secret created successfully.")
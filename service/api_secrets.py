from datetime import datetime
from fastapi import APIRouter, HTTPException
from models_secrets import Secret, NewSecret, SecretsResponse, SecretResponse, SecretDeleteResponse, SecretValueResponse, PODS_SERVICE_ACCOUNT, PODS_SERVICE_TENANT
from models_secret_logs import log_secret_event
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
from __init__ import t
from utils import check_permissions
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
    - See full documentation at: https://tapis.readthedocs.io/en/latest/technical/pods.html#secrets

    Returns a list of secrets (without secret values).
    """
    logger.info("GET /pods/secrets - Top of list_secrets.")

    # Get all secrets for this site
    secrets = Secret.db_get_all(tenant="siteadmintable", site=g.site_id)
    
    # Filter to only show secrets the user has READ+ permission to
    user_secrets = []
    for secret in secrets:
        if check_permissions(user=g.username, object=secret, object_type="secret", level=codes.READ, roles=g.roles):
            user_secrets.append(secret)
    
    secrets_to_show = [secret.display() for secret in user_secrets]

    logger.info(f"Secrets retrieved. Count: {len(secrets_to_show)}")
    return ok(result=secrets_to_show, msg="Secrets retrieved successfully.")


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
    - Secrets are stored securely in Tapis Security Kernel (SK).
    - Secret names are automatically namespaced: ``pods_{tenant}_user_{username}_{name}``
    - Only YOU can use this secret in pods unless you grant permissions.
    - Use ``secret_map`` in pod definitions to inject as environment variables.
    - All secret operations are permanently logged for audit purposes.
    - See full documentation at: https://tapis.readthedocs.io/en/latest/technical/pods.html#secrets

    Request Body Fields:
    - **secret_id** (required): Alphanumeric name with underscores/dashes allowed (max 100 chars)
    - **secret_value** (required): The actual secret value to store securely
    - **description** (optional): ASCII description (max 500 chars)
    - **scope** (optional): ``user`` (default) or ``pod`` - determines secret visibility
    - **pod_id** (optional): Required if scope is ``pod``, must be omitted if scope is ``user``
    - **read_write** (optional): ``read_write`` (default) or ``read`` - controls if value can be updated

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

    # Check if secret already exists
    existing_secret = None
    try:
        existing_secret = Secret.db_get_with_pk(new_secret.secret_id, tenant="siteadmintable", site=g.site_id)
    except Exception as e:
        logger.critical(f"Error checking existing secret: {e}")

    if existing_secret:
        # Secret exists - check if same author (allow update) or different author (block)
        if existing_secret.added_by == g.username and existing_secret.tenant_id == g.request_tenant_id:
            # Same author: update the existing secret with new value (SK overwrites latest)
            logger.info(f"Secret '{new_secret.secret_id}' exists for user '{g.username}'. Updating secret value.")
            
            try:
                # Store updated value in SK (overwrites existing)
                logger.debug(f"Storing secret in SK with name: {existing_secret.sk_secret_name}")
                t.sk.writeSecret(
                    secretType='user',
                    secretName=existing_secret.sk_secret_name,
                    tenant=PODS_SERVICE_TENANT,
                    user=PODS_SERVICE_ACCOUNT,
                    data={'secret_value': new_secret.secret_value},
                    _tapis_set_x_headers_from_service=True
                )
                logger.debug(f"Secret stored in SK successfully.")
            except Exception as e:
                logger.error(f"Failed to store secret in SK: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to store secret in Security Kernel: {str(e)}"
                )
            
            # Update existing DB entry with new timestamp and description if provided
            existing_secret.creation_ts = datetime.utcnow()
            if new_secret.description:
                existing_secret.description = new_secret.description
            existing_secret.db_update(tenant="siteadmintable", site=g.site_id)
            logger.debug(f"Secret updated in db. secret_id: {existing_secret.secret_id}")
            
            # Log the update event
            log_secret_event(
                event_type="SECRET_RECREATED",
                secret_id=existing_secret.secret_id,
                sk_secret_name=existing_secret.sk_secret_name,
                actor=g.username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id,
                details={"scope": existing_secret.scope, "pod_id": existing_secret.pod_id}
            )
            
            return ok(result=existing_secret.display(), msg="Secret updated successfully.")
        else:
            # Different author: block with helpful error message
            raise HTTPException(
                status_code=400,
                detail=f"Secret name '{new_secret.secret_id}' is already in use by another user. "
                       f"Please choose a different secret_id. Secret names must be unique across all users."
            )

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
        raise HTTPException(
            status_code=500,
            detail=f"Failed to store secret in Security Kernel: {str(e)}"
        )

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
        details={"scope": secret.scope, "pod_id": secret.pod_id}
    )

    return ok(result=secret.display(), msg="Secret created successfully.")
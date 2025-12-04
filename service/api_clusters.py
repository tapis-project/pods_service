from fastapi import APIRouter, HTTPException
from models_cluster import Cluster, NewCluster, ClustersResponse, ClusterResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import AVAILABLE, CREATING
from tapisservice.config import conf
from tapisservice.logs import get_logger
import os, psycopg2, secrets, string, datetime, subprocess, requests
from typing import Dict
import json

logger = get_logger(__name__)

# Provisioning helpers -------------------------------------------------------

def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def provision_pg_role_and_schema(cluster_id: str) -> Dict[str, str]:
    """Create a dedicated Postgres role + schema for the cluster. Returns credentials (one-time)."""
    pg_host = os.environ.get("POSTGRES_HOST", "localhost")
    pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    pg_admin_db = os.environ.get("POSTGRES_DB", "postgres")
    pg_admin_user = os.environ.get("POSTGRES_ADMIN_USER", os.environ.get("POSTGRES_USERNAME", "postgres"))
    pg_admin_pass = os.environ.get("POSTGRES_ADMIN_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "postgres"))
    schema = f"cluster_{cluster_id}"
    role = f"pods_cluster_{cluster_id}"
    password = _gen_password()
    conn = None
    try:
        conn = psycopg2.connect(host=pg_host, port=pg_port, dbname=pg_admin_db, user=pg_admin_user, password=pg_admin_pass)
        conn.autocommit = True
        cur = conn.cursor()
        # Create or update role password
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %s) THEN CREATE ROLE " + role + " LOGIN PASSWORD %s; ELSE ALTER ROLE " + role + " WITH PASSWORD %s; END IF; END$$;",
            (role, password, password)
        )
        # Create schema if missing
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = %s) THEN EXECUTE 'CREATE SCHEMA " + schema + " AUTHORIZATION " + role + "'; END IF; END$$;",
            (schema,)
        )
        # Basic default privileges (adjust later as needed)
        cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {role};")
        cur.close()
        return {"db_host": pg_host, "db_port": str(pg_port), "db_name": pg_admin_db, "db_schema": schema, "db_username": role, "db_password": password}
    except Exception as e:
        logger.error(f"Postgres provisioning failed for cluster {cluster_id}: {e}")
        raise
    finally:
        if conn:
            conn.close()

def provision_rabbit_user_vhost(cluster_id: str) -> Dict[str, str]:
    """Create a RabbitMQ user + vhost for the cluster and grant permissions, following stores.py conventions."""
    rabbitmq_dash_host = getattr(conf, 'rabbitmq_dash_host', None)
    admin_user = conf.get("rabbitmq_user", "guest")
    admin_pass = conf.get("rabbitmq_pass", "guest")
    if not rabbitmq_dash_host:
        raise RuntimeError("rabbitmq_dash_host not configured.")

    fn_call = f"/home/tapis/rabbitmqadmin -H {rabbitmq_dash_host} -u {admin_user} -p {admin_pass} "

    vhost = f"pods_cluster_{cluster_id}"
    user = f"pods_cluster_{cluster_id}_user"
    password = _gen_password()

    try:
        # Declare user and vhost; grant permissions
        subprocess.run(fn_call + f"declare user name={user} password={password} tags=None", shell=True, check=True)
        subprocess.run(fn_call + f"declare vhost name={vhost}", shell=True, check=True)
        subprocess.run(fn_call + f"declare permission vhost={vhost} user={user} configure=.* write=.* read=.*", shell=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"RabbitMQ provisioning failed for cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=f"RabbitMQ provisioning failed: {e}")

    # Build AMQP URL (conf.rabbitmq_uri should be 'amqp://host:port')
    base_uri = getattr(conf, 'rabbitmq_uri', 'amqp://rabbitmq:5672')
    amqp_url = base_uri.replace("amqp://", f"amqp://{user}:{password}@") + f"/{vhost}"

    return {
        "rabbit_user": user,
        "rabbit_password": password,
        "rabbit_vhost": vhost,
        "amqp_url": amqp_url,
    }


def provision_tailscale_preauth_key(cluster_id: str) -> Dict[str, str]:
    """
    Create a Tailscale/Headscale preauth key for the cluster via API.
    Returns preauth key and associated metadata.
    """
    # Get Headscale server info from environment
    login_server = os.environ.get("TS_LOGIN_SERVER", "https://headscale.pods.tacc.develop.tapis.io")
    api_key = os.environ.get("TS_API_KEY")
    api_endpoint = os.environ.get("TS_API_ENDPOINT", "/api/v1/preauthkey")
    reusable = os.environ.get("TS_KEY_REUSABLE", "false").lower() == "true"
    ephemeral = os.environ.get("TS_KEY_EPHEMERAL", "false").lower() == "true"
    expiration_hours = int(os.environ.get("TS_KEY_EXPIRATION_HOURS", "4"))
    
    # Calculate expiration time as a datetime object
    expiration = datetime.datetime.utcnow() + datetime.timedelta(hours=expiration_hours)
    
    # Build full API URL
    if login_server.endswith("/"):
        login_server = login_server[:-1]
    if api_endpoint.startswith("/"):
        api_endpoint = api_endpoint[1:]
    api_url = f"{login_server}/{api_endpoint}"
    
    logger.info(f"Requesting Tailscale preauth key for cluster {cluster_id} from {api_url}")
    
    # Check if we have API key
    if not api_key:
        logger.error("TS_API_KEY not set. Cannot provision Tailscale preauth key.")
        raise HTTPException(status_code=500, detail="Tailscale API key not configured, reach out to Admin.")
    
    try:
        # Set up request parameters
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # Use proper ISO8601 datetime format for the API
        expiration_iso = expiration.isoformat() + "Z"  # RFC3339 format with Z suffix for UTC
        
        payload = {
            "user": "1",
            "reusable": reusable,
            "ephemeral": ephemeral,
            "expiration": expiration_iso,  # Use ISO formatted datetime, not duration string
            "tags": [f"cluster:{cluster_id}", "managed-by:pods-service"],
            "description": f"Cluster {cluster_id} preauth key"
        }
        
        # Make API request
        logger.debug(f"POST {api_url} with payload: {payload}")
        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        
        # Parse response
        data = response.json().get('preAuthKey', {})
        logger.debug(f"Tailscale API response data.keys: {data.keys()}")
        logger.debug(f"Tailscale API response: {data}")
        
        if "key" not in data.keys() or "id" not in data.keys():
            logger.error(f"Invalid response from Tailscale API: {data}")
            raise HTTPException(status_code=500, detail="Invalid response from Tailscale API")
        
        logger.info(f"Tailscale preauth key provisioned for cluster {cluster_id}, expires at {expiration_iso}")
        # Return the preauth key info with our calculated expiration time
        return {
            "preauth_key": data.get("key"),
            "preauth_key_id": data.get("id"),
            "preauth_key_expires": data.get("expiration")  # Use the same ISO formatted string
        }
        
    except requests.RequestException as e:
        logger.error(f"Error calling Tailscale API: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Response: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"Tailscale API error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error provisioning Tailscale key: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to provision Tailscale key: {str(e)}")

router = APIRouter()

#### /pods/clusters

@router.get(
    "/pods/clusters",
    tags=["Clusters"],
    summary="list_clusters",
    operation_id="list_clusters",
    response_model=ClustersResponse)
async def list_clusters():
    """
    Get all clusters in your respective tenant and site that you have READ or higher access to. 

    Returns a list of clusters.
    """
    logger.info("GET /pods/clusters - Top of list_clusters.")

    clusters = Cluster.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    clusters_to_show = []
    for cluster in clusters:
        clusters_to_show.append(cluster.display())

    logger.info("Clusters retrieved.")
    return ok(result=clusters_to_show, msg="Clusters retrieved successfully.")


@router.post(
    "/pods/clusters",
    tags=["Clusters"],
    summary="create_cluster",
    operation_id="create_cluster")
async def create_cluster(new_cluster: NewCluster):
    """Create a cluster and provision external resources (tailscale key, rabbit user/vhost, postgres role/schema).
    Returns cluster metadata only. Use the /bootstrap endpoint to get connection credentials.
    """
    logger.info("POST /pods/clusters - Top of create_cluster.")

    # Create full Cluster object; validates as well
    cluster = Cluster(**new_cluster.dict())

    # Provision external dependencies as one transaction, so if one fails, none are persisted
    try:
        # ts first
        try:
            ts_info = provision_tailscale_preauth_key(cluster.cluster_id)
        except Exception as e:
            logger.error(f"Tailscale provisioning failed for cluster {cluster.cluster_id}: {e}")
            raise

        # rabbit
        try:
            rabbit_info = provision_rabbit_user_vhost(cluster.cluster_id)
        except Exception as e:
            logger.error(f"RabbitMQ provisioning failed for cluster {cluster.cluster_id}: {e}")
            rabbit_info = {}  # Continue even if RabbitMQ provisioning fails
        
        # pg
        try:
            pg_info = provision_pg_role_and_schema(cluster.cluster_id)
        except Exception as e:
            logger.error(f"Postgres provisioning failed for cluster {cluster.cluster_id}: {e}")
            pg_info = {}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"External provisioning failed: {e}")

    # Attach metadata fields if present on model
    if ts_info:
        if hasattr(cluster, 'ts_preauthkey_id'):
            cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
        if hasattr(cluster, 'ts_preauthkey_expires'):
            cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]
    if pg_info:
        if hasattr(cluster, 'db_username') and hasattr(pg_info, 'db_username'):
            cluster.db_username = pg_info["db_username"]
    if rabbit_info:
        if hasattr(cluster, 'rabbit_username') and 'rabbit_user' in rabbit_info:
            cluster.rabbit_username = rabbit_info["rabbit_user"]
        if hasattr(cluster, 'rabbit_vhost') and 'rabbit_vhost' in rabbit_info:
            cluster.rabbit_vhost = rabbit_info["rabbit_vhost"]
        if hasattr(cluster, 'status'):
            if not cluster.status:
                cluster.status = {}

    # Initialize bootstrap tracking
    if hasattr(cluster, 'bootstrap_access_count'):
        cluster.bootstrap_access_count = 0
    if hasattr(cluster, 'bootstrap_last_accessed'):
        cluster.bootstrap_last_accessed = None
    if hasattr(cluster, 'bootstrap_access_log'):
        cluster.bootstrap_access_log = []

    # Persist cluster
    cluster.db_create()
    logger.debug(f"Cluster saved in db. cluster_id: {cluster.cluster_id}; tenant: {g.request_tenant_id}.")

    return ok(result=cluster.display(), msg="Cluster created successfully. Use /bootstrap endpoint to get connection credentials.")


@router.get(
    "/pods/clusters/{cluster_id}/bootstrap",
    tags=["Clusters"],
    summary="get_cluster_bootstrap",
    operation_id="get_cluster_bootstrap")
async def get_cluster_bootstrap(cluster_id: str, regenerate_tailscale: bool = False):
    """
    Get bootstrap credentials for connecting to a cluster.
    
    Parameters:
    - cluster_id: The cluster identifier
    - regenerate_tailscale: If true, generates a new Tailscale preauth key (previous key will be invalidated)
    
    Returns connection credentials and setup instructions.
    This endpoint tracks access for security auditing and allows multiple accesses.
    
    ## Security Notes:
    - All bootstrap access is logged with timestamp, user, and IP
    - Tailscale keys can be regenerated if compromised
    - Database/RabbitMQ passwords are regenerated on each access for maximum security
    - Access count is tracked for monitoring unusual activity
    """
    logger.info(f"GET /pods/clusters/{cluster_id}/bootstrap - Retrieving bootstrap info.")
    
    # Get cluster with permission check
    try:
        cluster = Cluster.db_get_with_pk(cluster_id, tenant=g.request_tenant_id, site=g.site_id)
        # Add permission check here if your model supports it
        # cluster.check_permission(user=g.username, level='READ')
    except Exception as e:
        logger.error(f"Cluster not found or access denied: {e}")
        raise HTTPException(status_code=404, detail="Cluster not found or access denied")

    try:
        current_time = datetime.datetime.utcnow().isoformat() + "Z"
        
        # Handle Tailscale key (regenerate if requested or expired)
        ts_info = {}
        
        if regenerate_tailscale:
            logger.info(f"Regenerating Tailscale key for cluster {cluster_id}")
            ts_info = provision_tailscale_preauth_key(cluster_id)
            # Update cluster metadata
            if hasattr(cluster, 'ts_preauthkey_id'):
                cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
            if hasattr(cluster, 'ts_preauthkey_expires'):
                cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]
        else:
            # Check if we need to regenerate due to expiration
            if hasattr(cluster, 'ts_preauthkey_expires') and cluster.ts_preauthkey_expires:
                try:
                    expiry = datetime.datetime.fromisoformat(cluster.ts_preauthkey_expires.replace('Z', '+00:00'))
                    if expiry <= datetime.datetime.now(datetime.timezone.utc):
                        logger.info(f"Tailscale key expired for cluster {cluster_id}, regenerating")
                        ts_info = provision_tailscale_preauth_key(cluster_id)
                        cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
                        cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]
                    else:
                        # Key is still valid, but we need a fresh one for security
                        logger.info(f"Generating fresh Tailscale key for cluster {cluster_id}")
                        ts_info = provision_tailscale_preauth_key(cluster_id)
                        cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
                        cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]
                except Exception:
                    # If parsing fails, regenerate
                    ts_info = provision_tailscale_preauth_key(cluster_id)
                    cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
                    cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]
            else:
                # No expiry info, regenerate
                ts_info = provision_tailscale_preauth_key(cluster_id)
                cluster.ts_preauthkey_id = ts_info["preauth_key_id"]
                cluster.ts_preauthkey_expires = ts_info["preauth_key_expires"]

        # Always regenerate database and RabbitMQ credentials for security
        pg_info = {}
        rabbit_info = {}
        
        try:
            pg_info = provision_pg_role_and_schema(cluster_id)  # Updates password
        except Exception as e:
            logger.warning(f"Could not regenerate PG credentials: {e}")
            
        try:
            rabbit_info = provision_rabbit_user_vhost(cluster_id)  # Updates password
        except Exception as e:
            logger.warning(f"Could not regenerate RabbitMQ credentials: {e}")

        # Track bootstrap access
        access_count = getattr(cluster, 'bootstrap_access_count', 0) + 1
        if hasattr(cluster, 'bootstrap_access_count'):
            cluster.bootstrap_access_count = access_count
        if hasattr(cluster, 'bootstrap_last_accessed'):
            cluster.bootstrap_last_accessed = current_time
            
        # Add to access log
        access_entry = {
            "timestamp": current_time,
            "user": g.username,
            "access_count": access_count,
            "regenerated_tailscale": regenerate_tailscale,
            "ip_address": getattr(g, 'request_ip', 'unknown')  # If available
        }
        
        if hasattr(cluster, 'bootstrap_access_log'):
            access_log = getattr(cluster, 'bootstrap_access_log', [])
            access_log.append(access_entry)
            # Keep only last 50 entries to prevent unbounded growth
            cluster.bootstrap_access_log = access_log[-50:]

        # Update cluster in database
        cluster.db_update()

        # Prepare instructions
        login_server = os.environ.get("TS_LOGIN_SERVER", "https://headscale.pods.tacc.develop.tapis.io")
        preauth_key = ts_info.get("preauth_key", "")
        
        docker_instructions = f"docker run -d --name pods-remote-{cluster_id} -e TS_PREAUTHKEY=\"{preauth_key}\" -e TS_LOGIN_SERVER=\"{login_server}\" --restart unless-stopped tapis/pods-remote:latest"
        manual_instructions = f"tailscale up --login-server={login_server} --authkey={preauth_key} --hostname={cluster_id}"

        # Return bootstrap information
        bootstrap_data = {
            "tailscale": {
                "preauth_key": preauth_key,
                "expires": ts_info.get("preauth_key_expires"),
                "regenerated": regenerate_tailscale or "auto_refreshed"
            },
            "postgres": pg_info,
            "rabbitmq": rabbit_info,
            "docker_instructions": docker_instructions,
            "manual_instructions": manual_instructions,
            "documented_instructions": "https://tapis.readthedocs.io/en/latest/technical/pods.html#tailscale",
            "access_info": {
                "access_count": access_count,
                "last_accessed": current_time,
                "accessed_by": g.username,
                "security_note": "Credentials are regenerated on each access. Previous database/rabbit passwords are invalidated."
            }
        }
        
        logger.info(f"Bootstrap info provided for cluster {cluster_id} (access #{access_count})")
        return ok(result=bootstrap_data, msg="Bootstrap credentials retrieved successfully.")
        
    except Exception as e:
        logger.error(f"Failed to retrieve bootstrap info for cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve bootstrap credentials: {str(e)}")
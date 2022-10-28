from codes import ERROR, SPAWNER_SETUP, CREATING_CONTAINER, \
    REQUESTED, SHUTTING_DOWN
from models import Pod, Password
from kubernetes_utils import create_pod, create_service, create_pvc, KubernetesError
from kubernetes import client, config

from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
logger = get_logger(__name__)

# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def start_postgres_pod(pod, revision: int):
    logger.debug(f"Attempting to start postgres pod; name: {pod.k8_name}; revision: {revision}")

    password = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)

    # Volumes
    volumes = []
    volume_mounts = []

    # Create and mount certs neccessary for bolt TLS.
    secret_volume = client.V1SecretVolumeSource(secret_name='pods-certs')
    volumes.append(client.V1Volume(name='certs', secret = secret_volume))
    volume_mounts.append(client.V1VolumeMount(name="certs", mount_path="/etc/ssl/later"))

    container = {
        "name": pod.k8_name,
        "revision": revision,
        "image": "postgres",
        "command": ["docker-entrypoint.sh"],
        "args": [
          "-c", "ssl=on",
          "-c", "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",#"-c ssl_cert_file=/var/lib/postgresql/server.crt",
          "-c", "ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"#"-c ssl_key_file=/var/lib/postgresql/server.key"
        ],
        "ports_dict": {
            "postgres": 5432,
        },
        "environment": {
            "POSTGRES_USER": password.user_username,
            "POSTGRES_PASSWORD": password.user_password
        },
        "mounts": [volumes, volume_mounts],
        "mem_request": "250M",
        "cpu_request": "500",
        "mem_limit": "4G",
        "cpu_limit": "3000",
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = container["ports_dict"])


def start_neo4j_pod(pod, revision: int):
    logger.debug(f"Attempting to start neo4j pod; name: {pod.k8_name}; revision: {revision}")

    password = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)

    # Volumes
    volumes = []
    volume_mounts = []

    # Create and mount certs neccessary for bolt TLS.
    secret_volume = client.V1SecretVolumeSource(secret_name='pods-certs')
    volumes.append(client.V1Volume(name='certs', secret = secret_volume))
    volume_mounts.append(client.V1VolumeMount(name="certs", mount_path="/certificates/bolt"))

    # Init new user/pass https://neo4j.com/labs/apoc/4.1/operational/init-script/
    container = {
        "name": pod.k8_name,
        "revision": revision,
        "image": "neo4j:4.4",
        "command": [
            '/bin/bash',
            '-c',
            ('export NEO4J_dbms_default__advertised__address=$(hostname -f) && '
             'exec /docker-entrypoint.sh "neo4j"')
        ],
        "ports_dict": {
            "browser": 7474,
            "bolt": 7687
        },
        "environment": {
            "NEO4JLABS_PLUGINS": '["apoc", "n10s"]',
            "NEO4J_dbms_ssl_policy_bolt_enabled": "true",
            "NEO4J_dbms_ssl_policy_bolt_base__directory": "/certificates/bolt", # Can't mount anything to /var/lib/neo4j. Neo4j attempts chown, read-only. So change dir.
            "NEO4J_dbms_ssl_policy_bolt_private__key": "tls.key",
            "NEO4J_dbms_ssl_policy_bolt_public__certificate": "tls.crt",
            "NEO4J_dbms_ssl_policy_bolt_client__auth": "NONE",
            "NEO4J_dbms_security_auth__enabled": "true",
            "NEO4J_dbms_mode": "SINGLE",
            "NEO4J_apoc_import_file_enabled": "true",
            "NEO4J_apoc_export_file_enabled": "true",
            # Create users here with env and apoc. Different format than Neo4J. Kinda borked, might change. github.com/neo4j-contrib/neo4j-apoc-procedures/issues/2120
            # Pods admin user
            "apoc.initializer.system.1": f"CREATE USER {password.admin_username} SET PLAINTEXT PASSWORD '{password.admin_password}' SET PASSWORD CHANGE NOT REQUIRED",
            # Users user
            "apoc.initializer.system.2": f"CREATE USER {password.user_username} SET PLAINTEXT PASSWORD '{password.user_password}' SET PASSWORD CHANGE NOT REQUIRED"
        },
        "mounts": [volumes, volume_mounts],
        "mem_request": "250M",
        "cpu_request": "500",
        "mem_limit": "4G",
        "cpu_limit": "3000",
        "user": None
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = container["ports_dict"])


def start_generic_pod(pod, custom_image, revision: int):
    logger.debug(f"Attempting to start generic pod; name: {pod.k8_name}; revision: {revision}")

    # Volumes
    volumes = []
    volume_mounts = []

    # Create PVC if requested.
    if pod.persistent_volume:
        create_pvc(name = pod.k8_name)
        persistent_volume = client.V1PersistentVolumeClaimVolumeSource(claim_name=pod.k8_name)
        volumes.append(client.V1Volume(name='user-volume', persistent_volume_claim = persistent_volume))
        volume_mounts.append(client.V1VolumeMount(name="user-volume", mount_path="/user_volume"))

    container = {
        "name": pod.k8_name,
        "command": pod.command,
        "revision": revision,
        "image": custom_image,
        "ports_dict": {
            "http": pod.routing_port
        },
        "environment": pod.environment_variables.copy(),
        "mounts": [volumes, volume_mounts],
        "mem_request": "250M",
        "cpu_request": "500",
        "mem_limit": "4G",
        "cpu_limit": "3000",
        "user": None
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = container["ports_dict"])

from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set, Optional
from wsgiref import validate
from pydantic import BaseModel, Field, validator, model_validator, conint, create_model
from codes import PermissionLevel

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from __init__ import t

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from models_base import TapisApiModel, TapisModel, HealthcheckProbe, PodHealthchecks
from models_templates import Template
from models_misc import PermissionsModel, CredentialsModel, LogsModel
from models_images import Image
from models_volumes import Volume
from models_snapshots import Snapshot
from models_volume_mounts_utils import is_volume_placeholder
from models_volume_mounts_utils import VolumeMount, validate_and_convert_volume_mounts, VALID_VOLUME_MOUNT_TYPES
from typing import Optional


def derive_template_info(input_template_name, update_template_tag: bool = False, tenant: str = g.request_tenant_id, site: str = g.site_id):
    # template is in the format template_id:template_tag@2024-06-10-17:20:27
    # template_id is required, template_tag and timestamp are optional
    # If no template_tag, default to latest
    # If no timestamp, derive and use latest timestamp
    template_id = None
    template_tag = None
    tag_timestamp = None
    derived_template_tag = None
    if "@" in input_template_name:
        # we expect template_id:template_tag if @ is present
        template_id_n_tag, tag_timestamp = input_template_name.split("@", 1)
        if ":" in template_id_n_tag:
            parts = template_id_n_tag.split(":")
            if len(parts) != 2:
                raise ValueError(f"Invalid template format: '{input_template_name}'. Expected 'template_id:tag@timestamp'. Got {len(parts)} parts separated by ':'.")
            template_id, template_tag = parts
        else:
            raise ValueError(f"Invalid template format: '{input_template_name}'. When using '@' for timestamp, format must be 'template_id:tag@timestamp'.")
    elif ":" in input_template_name:
        # If no @, we expect template_id:template_tag if : is present
        parts = input_template_name.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid template format: '{input_template_name}'. Expected 'template_id:tag'. Got {len(parts)} parts separated by ':'.")
        template_id, template_tag = parts
    else:
        template_id = input_template_name

    logger.debug(f"Top of derive_template_info for input template: {input_template_name}, template_id: {template_id}, template_tag: {template_tag}, tenant: {tenant}, site: {site}")
    ## template_id check
    template = Template.db_get_with_pk(template_id, tenant="siteadmintable", site=site)
    if not template:
        raise ValueError(f"Template not found: '{template_id}'. Verify template_id exists.")
    if not template_tag:
        # If no template_tag, we'll use the latest tag.
        template_tag = "latest"

    ## template_tag check
    if template_tag and tag_timestamp:
        full_tag = f"{template_tag}@{tag_timestamp}"
        template_tags = TemplateTag.db_get_where(where_params=[['tag_timestamp', '.eq', str(full_tag)]], sort_column='creation_ts', tenant="siteadmintable", site=site)
        if not template_tags:
            raise ValueError(f"Template tag not found: '{input_template_name}'. Verify template_id, tag, and timestamp all exist.")
        if len(template_tags) > 1:
            raise ValueError(f"Multiple template tags found for '{input_template_name}'. This should not happen - contact admin.")
        derived_template_tag = template_tags[0]
    elif not tag_timestamp:
        # timestamp not provided, we'll look for matching tags and set tag_timestamp to the most recent.
        template_tags = TemplateTag.db_get_where(where_params=[['tag', '.eq', template_tag]], sort_column='creation_ts', tenant="siteadmintable", site=site)
        if not template_tags:
            raise ValueError(f"Template tag not found: '{template_id}:{template_tag}'. Verify template_id and tag both exist.")
        # found matching tags, get the most recent one.
        derived_template_tag = template_tags[0]
        _, tag_timestamp = derived_template_tag.tag_timestamp.split("@")

    logger.debug(f"End of derive_template_info for template: {input_template_name}, tenant: {tenant}, site: {site}")
    return f"{template_id}:{template_tag}@{tag_timestamp}", template, derived_template_tag


class Networking(TapisModel):
    protocol: str =  Field("http", description = "Which network protocol to use. `http`, `tcp`, `postgres`, or `local_only`. `local_only` is only accessible from within the cluster.")
    port: int = Field(5000, description = "Pod port to expose via networking.url in this networking object.")
    url: str = Field("", description = "URL used to access the port of the pod defined in this networking object. Generated by service.")
    ip_allow_list: list[str] = Field([], description = "List of IPs that are allowed to access this specific pod port. If empty, all IPs are allowed. ex. ['127.0.0.1/32', '192.168.1.7']")
    tapis_auth: bool = Field(False, description = "If true, will require Tapis auth to access the pod.")
    tapis_auth_response_headers: Dict[str, str] = Field({}, description = "Specification of headers to forward to the pod when using Tapis auth.")
    tapis_auth_allowed_users: list[str] = Field(["AUTHORIZED_USERS"], description = "List of users allowed to access the pod when using Tapis auth. Supports literal usernames, '*' (all users), and permission-based groups: 'AUTHORIZED_READS' (READ+), 'AUTHORIZED_USERS' (USER+), 'AUTHORIZED_ADMINS' (ADMIN+, includes APPROVEDADMIN). Groups resolve against the pod's permissions list.")
    tapis_auth_return_path: str = Field("/", description = "Path to redirect to when accessing the pod via Tapis auth.")
    tapis_auth_excluded_paths: list[str] = Field([], description = "List of PathPrefix patterns to exclude from Tapis auth (no forwardAuth). Useful for static assets that don't need auth. ex. ['/assets', '/static', '/_app']")
    tapis_auth_excluded_path_regex: list[str] = Field([], description = "List of PathRegexp patterns to exclude from Tapis auth. ex. ['\\\\.(js|css|ico|png|jpg|jpeg|webp|gif|svg|woff2?|ttf|map)$']")
    cors_allow_origins: list[str] = Field([], description = "List of CORS allowed origins. ex. ['https://tacc.develop.tapis.io', 'https://tacc.tapis.io']")
    cors_allow_methods: list[str] = Field([], description = "List of CORS allowed methods. ex. ['GET', 'POST', 'PUT', 'DELETE']")
    cors_allow_headers: list[str] = Field([], description = "List of CORS allowed headers. ex. ['Content-Type', 'X-Tapis-Token']")
    cors_allow_credentials: bool = Field(False, description = "Boolean to allow credentials to be sent with CORS requests.")
    cors_max_age: int = Field(100, description = "Max age of CORS preflight requests in seconds.")
    tapis_ui_uri: str = Field("", description = "Path to redirect to when accessing the pod via Tapis UI.")
    tapis_ui_uri_redirect: bool = Field(False, description = "If true, will redirect to the tapis_ui_uri when accessing the pod via Tapis UI. Otherwise, just read-only uri.")
    tapis_ui_uri_description: str = Field("", description = "Describing where the tapis_ui_uri will redirect to.")
    # Proxy compression (Traefik compress middleware)
    proxy_compression: bool = Field(True, description = "Enable Traefik compress middleware for this HTTP endpoint. Enabled by default for all HTTP protocol endpoints.")
    proxy_compression_encodings: list[str] = Field(["zstd", "br", "gzip"], description = "Ordered list of compression encodings by priority. Valid values: 'zstd', 'br', 'gzip'.")
    proxy_compression_excluded_content_types: list[str] = Field([], description = "Content types to exclude from compression. Already-compressed formats (images, video, archives) are excluded automatically. Set explicit types here to add additional exclusions.")
    proxy_compression_min_response_body_bytes: int = Field(1024, description = "Minimum response body size in bytes before compression is applied. Responses smaller than this are not compressed.")

    @validator('protocol')
    def check_protocol(cls, v):
        v = v.lower()
        valid_protocols = ['http', 'tcp', 'postgres', 'local_only']
        if v not in valid_protocols:
            raise ValueError(f"networking.protocol must be one of the following: {valid_protocols}.")
        return v

    @validator('port')
    def check_port(cls, v):
        if 10 > v  or v > 99999:
            raise ValueError(f"networking.port must be an int with 2 to 5 digits. Got port: {v}")
        return v

    @validator('url')
    def check_url(cls, v):
        if v:
            # Regex match to ensure url is safe with only [A-z0-9.-] chars.
            res = re.fullmatch(r'[a-z][a-z0-9.-]+', v)
            if not res:
                raise ValueError(f"networking.url can only contain lowercase alphanumeric characters, periods, and hyphens.")
            # pod_id char limit = 64
            if len(v) > 128:
                raise ValueError(f"networking.*.url length must be below 128 characters. Inputted length: {len(v)}")
        return v

    @validator('tapis_auth_allowed_users')
    def check_tapis_auth_allowed_users(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"tapis_auth_allowed_users must be list. Got '{type(v).__name__}'.")
            for user in v:
                if not isinstance(user, str):
                    raise TypeError(f"tapis_auth_allowed_users must be list of str. Got '{type(user).__name__}'.")
        return v

    @validator('tapis_auth_excluded_paths')
    def check_tapis_auth_excluded_paths(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.tapis_auth_excluded_paths must be list. Got '{type(v).__name__}'.")
            if len(v) > 50:
                raise ValueError(f"networking.tapis_auth_excluded_paths must have at most 50 entries. Got {len(v)}.")
            for path in v:
                if not isinstance(path, str):
                    raise TypeError(f"networking.tapis_auth_excluded_paths must be list of str. Got '{type(path).__name__}'.")
                if not path.startswith('/'):
                    raise ValueError(f"networking.tapis_auth_excluded_paths values must start with '/'. Got '{path}'.")
                if not path.isascii():
                    raise ValueError(f"networking.tapis_auth_excluded_paths values must be ASCII. Got '{path}'.")
                if len(path) > 256:
                    raise ValueError(f"networking.tapis_auth_excluded_paths values must be less than 256 characters. Got length {len(path)}.")
        return v

    @validator('tapis_auth_excluded_path_regex')
    def check_tapis_auth_excluded_path_regex(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.tapis_auth_excluded_path_regex must be list. Got '{type(v).__name__}'.")
            if len(v) > 20:
                raise ValueError(f"networking.tapis_auth_excluded_path_regex must have at most 20 entries. Got {len(v)}.")
            for pattern in v:
                if not isinstance(pattern, str):
                    raise TypeError(f"networking.tapis_auth_excluded_path_regex must be list of str. Got '{type(pattern).__name__}'.")
                if not pattern.isascii():
                    raise ValueError(f"networking.tapis_auth_excluded_path_regex values must be ASCII. Got '{pattern}'.")
                if len(pattern) > 512:
                    raise ValueError(f"networking.tapis_auth_excluded_path_regex values must be less than 512 characters. Got length {len(pattern)}.")
                # Validate the regex compiles
                try:
                    import re as _re
                    _re.compile(pattern)
                except _re.error as e:
                    raise ValueError(f"networking.tapis_auth_excluded_path_regex contains invalid regex '{pattern}': {e}")
        return v

    @validator('tapis_ui_uri')
    def check_tapis_ui_uri(cls, v):
        if v:
            # must start with /
            if not v.startswith('/'):
                raise ValueError(f"networking.tapis_ui_uri must start with '/'. Got {v}")
            # Regex match to ensure url is safe with only [A-z0-9.-/] chars.
            res = re.fullmatch(r'[\/]+[a-z][a-z0-9.\-\/]+', v)
            if not res:
                raise ValueError(f"networking.tapis_ui_uri can only contain lowercase alphanumeric characters, periods, forward-slash, and hyphens. Must begin with /. Got {v}")
            # pod_id char limit = 64
            if len(v) > 128:
                raise ValueError(f"networking.tapis_ui_uri length must be below 128 characters. Inputted length: {len(v)}")
        return v
    
    @validator('tapis_ui_uri_description')
    def check_tapis_ui_uri_description(cls, v):
        # ensure tapis_ui_uri_description is all ascii
        if not v.isascii():
            raise ValueError(f"tapis_ui_uri_description field may only contain ASCII characters.")
        # make sure tapis_ui_uri_description < 255 characters
        if len(v) > 255:
            raise ValueError(f"tapis_ui_uri_description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    # create validators for the 5 cors fields
    @validator('cors_allow_origins')
    def check_cors_allow_origins(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.cors_allow_origins must be list. Got '{type(v).__name__}'.")
            for origin in v:
                if not isinstance(origin, str):
                    raise TypeError(f"networking.cors_allow_origins must be list of str. Got '{type(origin).__name__}'.")
        return v
    
    @validator('cors_allow_methods')
    def check_cors_allow_methods(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.cors_allow_methods must be list. Got '{type(v).__name__}'.")
            for method in v:
                if not isinstance(method, str):
                    raise TypeError(f"networking.cors_allow_methods must be list of str. Got '{type(method).__name__}'.")
        return v

    @validator('cors_allow_headers')
    def check_cors_allow_headers(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.cors_allow_headers must be list. Got '{type(v).__name__}'.")
            for header in v:
                if not isinstance(header, str):
                    raise TypeError(f"networking.cors_allow_headers must be list of str. Got '{type(header).__name__}'.")
        return v

    @validator('cors_allow_credentials')
    def check_cors_allow_credentials(cls, v):
        if v:
            if not isinstance(v, bool):
                raise TypeError(f"networking.cors_allow_credentials must be bool. Got '{type(v).__name__}'.")
        return v

    @validator('cors_max_age')
    def check_cors_max_age(cls, v):
        if v:
            if not isinstance(v, int):
                raise TypeError(f"networking.cors_max_age must be int. Got '{type(v).__name__}'.")
            if v < 0:
                raise ValueError(f"networking.cors_max_age must be greater than 0. Got {v}")
            # max is 100s
            if v > 100:
                raise ValueError(f"networking.cors_max_age must be less than 100 seconds. Got {v}")
        return v

    @validator('ip_allow_list')
    def check_ip_allow_list(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.ip_allow_list must be list. Got '{type(v).__name__}'.")
            for ip in v:
                if not isinstance(ip, str):
                    raise TypeError(f"networking.ip_allow_list must be list of str. Got '{type(ip).__name__}'.")
                ## ensure ip 123.232.323/xx. Return error if * is str as that's not allowed.
                if ip == "*":
                    raise ValueError(f"networking.ip_allow_list cannot contain '*'. All values must be valid IPs or CIDR ranges.")
                # Regex match to ensure ip is safe with only [0-9./] chars.
                res = re.fullmatch(r'(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?', ip)
                if not res:
                    raise ValueError(f"networking.ip_allow_list can only contain valid IPs or CIDR ranges. Got {ip}")
        return v

    @validator('proxy_compression_encodings')
    def check_proxy_compression_encodings(cls, v):
        valid_encodings = ['zstd', 'br', 'gzip']
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.proxy_compression_encodings must be list. Got '{type(v).__name__}'.")
            for enc in v:
                if not isinstance(enc, str):
                    raise TypeError(f"networking.proxy_compression_encodings must be list of str. Got '{type(enc).__name__}'.")
                if enc not in valid_encodings:
                    raise ValueError(f"networking.proxy_compression_encodings values must be one of {valid_encodings}. Got '{enc}'.")
            if len(v) == 0:
                raise ValueError(f"networking.proxy_compression_encodings must have at least one encoding when proxy_compression is enabled.")
            if len(v) != len(set(v)):
                raise ValueError(f"networking.proxy_compression_encodings must not contain duplicates. Got {v}.")
        return v

    @validator('proxy_compression_excluded_content_types')
    def check_proxy_compression_excluded_content_types(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"networking.proxy_compression_excluded_content_types must be list. Got '{type(v).__name__}'.")
            for ct in v:
                if not isinstance(ct, str):
                    raise TypeError(f"networking.proxy_compression_excluded_content_types must be list of str. Got '{type(ct).__name__}'.")
                if not ct.isascii():
                    raise ValueError(f"networking.proxy_compression_excluded_content_types values must be ASCII. Got '{ct}'.")
                if len(ct) > 128:
                    raise ValueError(f"networking.proxy_compression_excluded_content_types values must be less than 128 characters. Got length {len(ct)}.")
            if len(v) > 50:
                raise ValueError(f"networking.proxy_compression_excluded_content_types must have at most 50 entries. Got {len(v)}.")
        return v

    @validator('proxy_compression_min_response_body_bytes')
    def check_proxy_compression_min_response_body_bytes(cls, v):
        if not isinstance(v, int):
            raise TypeError(f"networking.proxy_compression_min_response_body_bytes must be int. Got '{type(v).__name__}'.")
        if v < 0:
            raise ValueError(f"networking.proxy_compression_min_response_body_bytes must be >= 0. Got {v}.")
        if v > 10485760:  # 10 MiB max
            raise ValueError(f"networking.proxy_compression_min_response_body_bytes must be <= 10485760 (10 MiB). Got {v}.")
        return v

    @model_validator(mode="after")
    def check_tapis_protocol_with_configured_options(cls, values):
        protocol = getattr(values, 'protocol', None)
        tapis_auth = getattr(values, 'tapis_auth', None)
        # cors too are http only
        cors_allow_origins = getattr(values, 'cors_allow_origins', None)
        cors_allow_methods = getattr(values, 'cors_allow_methods', None)
        cors_allow_headers = getattr(values, 'cors_allow_headers', None)
        cors_allow_credentials = getattr(values, 'cors_allow_credentials', None)
        cors_max_age = getattr(values, 'cors_max_age', None)

        if tapis_auth and protocol != "http":
            raise ValueError(f"networking.tapis_auth can only be used with protocol 'http'. Got protocol {protocol}.")

        if cors_allow_origins:
            if protocol != "http":
                raise ValueError(f"networking.cors_* can only be used with protocol 'http'. Got protocol {protocol}.")

        # Silently disable proxy_compression for non-http protocols (don't error, for backwards compat)
        proxy_compression = getattr(values, 'proxy_compression', None)
        if proxy_compression and protocol != "http":
            object.__setattr__(values, 'proxy_compression', False)

        # Silently clear tapis_auth exclusion paths for non-http protocols
        if protocol != "http":
            tapis_auth_excluded_paths = getattr(values, 'tapis_auth_excluded_paths', None)
            tapis_auth_excluded_path_regex = getattr(values, 'tapis_auth_excluded_path_regex', None)
            if tapis_auth_excluded_paths:
                object.__setattr__(values, 'tapis_auth_excluded_paths', [])
            if tapis_auth_excluded_path_regex:
                object.__setattr__(values, 'tapis_auth_excluded_path_regex', [])

        return values



class Resources(TapisModel):
    # CPU/Mem defaults are set in configschema.json
    # CPU
    cpu_request: int | None = Field(None, description = "CPU allocation pod requests at startup. In millicpus (m). 1000 = 1 cpu.")
    cpu_limit: int | None = Field(None, description = "CPU allocation pod is allowed to use. In millicpus (m). 1000 = 1 cpu.")
    # Mem
    mem_request: int | None = Field(None, description = "Memory allocation pod requests at startup. In mebibytes (Mi)")
    mem_limit: int | None = Field(None, description = "Memory allocation pod is allowed to use. In mebibytes (Mi)")
    # Ephemeral Storage
    ephemeral_storage_request: int | None = Field(None, description = "Ephemeral storage pod requests at startup. In mebibytes (Mi)")
    ephemeral_storage_limit: int | None = Field(None, description = "Ephemeral storage pod is allowed to use. In mebibytes (Mi)")
    # GPU
    gpus: int | None = Field(None, description = "GPU allocation pod is allowed to use. In integers of GPUs. (we only have 1 currently ;) )")

    @validator('cpu_request', 'cpu_limit')
    def check_cpu_resources(cls, v):
        if not v:
            return v
        if conf.minimum_pod_cpu_val > v  or v > conf.maximum_pod_cpu_val:
            raise ValueError(
                f"resources.cpu_x out of bounds. Received: {v}. Maximum: {conf.maximum_pod_cpu_val}. Minimum: {conf.minimum_pod_cpu_val}.",
                 " User requires extra role to break bounds. Contact admin."
                )
        return v

    @validator('mem_request', 'mem_limit')
    def check_mem_resources(cls, v):
        if not v:
            return v
        if conf.minimum_pod_mem_val > v  or v > conf.maximum_pod_mem_val:
            raise ValueError(
                f"resources.mem_x out of bounds. Received: {v}. Maximum: {conf.maximum_pod_mem_val}. Minimum: {conf.minimum_pod_mem_val}.",
                 " User requires extra role to break bounds. Contact admin."
                )
        return v

    @validator('ephemeral_storage_request', 'ephemeral_storage_limit')
    def check_ephemeral_storage_resources(cls, v):
        if not v:
            return v
        # Allow -1 (unlimited) only if the default is also -1 (admin has enabled unlimited)
        if v == -1:
            if conf.default_pod_ephemeral_storage_request == -1 or conf.default_pod_ephemeral_storage_limit == -1:
                return v
            else:
                raise ValueError(
                    f"resources.ephemeral_storage_x: -1 (unlimited) is not allowed. "
                    f"Admin has not enabled unlimited ephemeral storage (defaults are not -1). "
                    f"Use values between {conf.minimum_pod_ephemeral_storage_val} and {conf.maximum_pod_ephemeral_storage_val}."
                )
        if conf.minimum_pod_ephemeral_storage_val > v  or v > conf.maximum_pod_ephemeral_storage_val:
            raise ValueError(
                f"resources.ephemeral_storage_x out of bounds. Received: {v}. Maximum: {conf.maximum_pod_ephemeral_storage_val}. Minimum: {conf.minimum_pod_ephemeral_storage_val}. "
                f"User requires extra role to break bounds. Contact admin."
                )
        return v

    @validator('gpus')
    def check_gpus(cls, v):
        if not v:
            return v
        if 0 > v  or v > conf.maximum_pod_gpu_val:
            raise ValueError(
                f"resources.gpus out of bounds. Received: {v}. Maximum: {conf.maximum_pod_gpu_val}. Minimum: 0.",
                 " User requires extra role to break bounds. Contact admin."
                )
        return v

    @model_validator(mode="after")
    def ensure_request_lessthan_limit(cls, values):
        cpu_request = getattr(values, "cpu_request", None)
        cpu_limit = getattr(values, "cpu_limit", None)
        mem_request = getattr(values, "mem_request", None)
        mem_limit = getattr(values, "mem_limit", None)
        ephemeral_storage_request = getattr(values, 'ephemeral_storage_request')
        ephemeral_storage_limit = getattr(values, 'ephemeral_storage_limit')    
        gpus = getattr(values, "gpus", None) # There's no request/limit for gpus, just an int validated in check_gpus

        # Check cpu values
        if cpu_request and cpu_limit and cpu_request > cpu_limit:
            raise ValueError(f"resources.cpu_x found cpu_request({cpu_request}) > cpu_limit({cpu_limit}). Request must be less than or equal to limit.")

        # Check mem values
        if mem_request and mem_limit and mem_request > mem_limit:
            raise ValueError(f"resources.mem_x found mem_request({mem_request}) > mem_limit({mem_limit}). Request must be less than or equal to limit.")

        # Check ephemeral storage values (skip if either is -1, which means unlimited/unset)
        if ephemeral_storage_request and ephemeral_storage_limit and ephemeral_storage_request != -1 and ephemeral_storage_limit != -1:
            if ephemeral_storage_request > ephemeral_storage_limit:
                raise ValueError(f"resources.ephemeral_storage_x found ephemeral_storage_request({ephemeral_storage_request}) > ephemeral_storage_limit({ephemeral_storage_limit}). Request must be less than or equal to limit. Use -1 for unlimited.")

        return values


class TemplateTagPodDefinition(TapisModel):
    # All fields are optional and default to None or empty objects for easier parsing of modified fields later
    # Optional
    image: str | None = Field(None, description = "Which docker image to use, must be on allowlist, check /pods/images for list.")
    template: str | None = Field(None, description = "Name of template to base this template off of.")
    description: str | None= Field(None, description = "Description of this pod.")
    command: List[str] | None = Field(None, description = 'Command to run in pod. ex. `["sleep", "5000"]` or `["/bin/bash", "-c", "(exec myscript.sh)"]`', sa_column=Column(ARRAY(String)))
    arguments: List[str] | None = Field(None, description = "Arguments for the Pod's command.", sa_column=Column(ARRAY(String)))
    environment_variables: Dict[str, Any] = Field({}, description = "Environment variables to inject into pod. Use `${pods:secrets:KEY}` to reference secret_map entries.", sa_column=Column(JSON))
    secret_map: Dict[str, str] = Field({}, description = "Map of keys to secret references or placeholders. Use ${secret:name} for user secrets, ${pods:default:val:?desc} for placeholders with defaults, ${:?desc} for required placeholders. Secrets resolved at pod start.", sa_column=Column(JSON))
    volume_mounts: Dict[str, Optional[VolumeMount]] = Field(
        {}, 
        description = 'Volume mounts keyed by mount_path. For templates, tapisvolume/tapissnapshot MUST use placeholder source_id (e.g., "${:?Description}"). Ex: {"/data": {"type": "tapisvolume", "source_id": "${:?User data volume}"}, "/etc/config.ini": {"type": "ephemeral", "config_content": "key=value"}}',
        sa_column=Column(JSON)
    )
    time_to_stop_default: int | None = Field(None, description = "Default time (sec) for pod to run from instance start. -1 for unlimited. 12 hour default.")
    time_to_stop_instance: int | None = Field(None, description = "Time (sec) for pod to run from instance start. Reset each time instance is started. -1 for unlimited. None uses default.")
    networking: Dict[str, Networking] = Field({}, description = 'Networking information. `{"url_suffix": {"protocol": "http"  "tcp", "port": int}}`', sa_column=Column(JSON))
    resources: Resources = Field({}, description = 'Pod resource management `{"cpu_limit": 3000, "mem_limit": 3000, "cpu_request": 500, "mem_limit": 500, "gpus": 0}`', sa_column=Column(JSON))
    compute_queue: str = Field("default", description = "Queue to run pod in. `default` is the default queue.")
    healthchecks: PodHealthchecks | None = Field(None, description = 'Kubernetes health probe configuration inherited by pods using this template. Supports liveness, readiness, and startup probes.')

    @validator('template')
    def check_template(cls, v):
        if v:
            template_name_str, template, template_tag = derive_template_info(v, tenant=g.tenant_id, site=g.site_id)
            return template_name_str
        else:
            return v

    @validator('description')
    def check_description(cls, v):
        if not v:
            return v
        # ensure description is all ascii
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")            
        # make sure description < 255 characters
        if len(v) > 255:
            raise ValueError(f"description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    @validator('environment_variables')
    def check_environment_variables(cls, v):
        if v:
            if not isinstance(v, dict):
                raise TypeError(f"environment_variable must be dict. Got {type(v).__name__}.")
            for env_key, env_val in v.items():
                if not isinstance(env_key, str):
                    raise TypeError(f"environment_variable key must be str. Got {type(env_key).__name__}.")
                if not isinstance(env_val, str):
                    raise TypeError(f"environment_variable val must be str. Got {type(env_val).__name__}.")
        return v

    @validator('secret_map')
    def check_secret_map(cls, v):
        """Validate secret_map format at template level.
        
        Template secret_map can contain (NO direct secret references):
        - Placeholders with defaults: ${pods:default:value:?description}
        - Required placeholders: ${:?description}
        - Literal strings for non-secret configuration values
        
        Pod creators override placeholders with their own ${secret:name} references.
        """
        if not v:
            return v
        if not isinstance(v, dict):
            raise TypeError(f"secret_map must be dict. Got {type(v).__name__}.")
        for key, value in v.items():
            if not isinstance(key, str):
                raise TypeError(f"secret_map key must be str. Got {type(key).__name__}.")
            if not isinstance(value, str):
                raise TypeError(f"secret_map value must be str. Got {type(value).__name__}.")
            # Basic format check - key should be alphanumeric with underscores/hyphens
            if not key.replace('_', '').replace('-', '').isalnum():
                raise ValueError(f"secret_map key must be alphanumeric and may include '_' or '-'. Got: {key}")
        return v

    @validator('volume_mounts', pre=True)
    def check_volume_mounts(cls, v):
        """Validate volume_mounts dict structure (keyed by mount_path)."""
        if not v:
            return v
        
        # Use consolidated validation function (handles legacy list format + VolumeMount validation)
        return validate_and_convert_volume_mounts(v, use_full_validation=True)

    @model_validator(mode="after")
    def check_volume_mounts_db(cls, values):
        """Validate that referenced volumes/snapshots exist in database.
        
        Note: Placeholders (${:?description}) are skipped - they're validated
        separately and resolved when pods are created from templates.
        """
        volume_mounts = getattr(values, 'volume_mounts', None)
        tenant_id = g.tenant_id if hasattr(g, 'tenant_id') else None
        site_id = g.site_id if hasattr(g, 'site_id') else None
        
        if volume_mounts and tenant_id and tenant_id != "" and site_id and site_id != "":
            for mount_path, mount_config in volume_mounts.items():
                # Skip null mounts (used to remove template mounts)
                if mount_config is None:
                    continue
                
                # Handle both dict and VolumeMount object
                if isinstance(mount_config, dict):
                    vol_type = mount_config.get('type', '').lower()
                    source_id = mount_config.get('source_id', '')
                else:
                    vol_type = (getattr(mount_config, 'type', '') or '').lower()
                    source_id = getattr(mount_config, 'source_id', '') or ''
                
                # For tapisvolume and tapissnapshot in templates, source_id must be a placeholder
                # Templates define structure, pods provide actual volume bindings
                if vol_type in ("tapisvolume", "tapissnapshot"):
                    if source_id and not is_volume_placeholder(source_id):
                        raise ValueError(
                            f"Template volume_mounts['{mount_path}'] has literal source_id '{source_id}'. "
                            f"Templates must use placeholders for {vol_type} mounts (e.g., '${{:?Description of volume needed}}'). "
                            f"Pod creators will override with their own volume IDs."
                        )
                
                # 'ephemeral' doesn't need db validation - config is inline
                # 'pvc' type is admin-only and may use literal PVC names
        
        return values

    @validator('arguments')
    def check_arguments(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"arguments must be list. Got {type(v).__name__}.")
            for arg in v:
                if not isinstance(arg, str):
                    raise TypeError(f"arguments must be list of str. Got {type(arg).__name__}.")
        return v

    @validator('command')
    def check_command(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"command must be list. Got {type(v).__name__}.")
            for arg in v:
                if not isinstance(arg, str):
                    raise TypeError(f"command must be list of str. Got {type(arg).__name__}.")
        return v

    @validator('image')
    def check_image(cls, v):
        # Template tag doesn't require image.
        if not v:
            return v
        if v.count(":") > 1:
            raise ValueError("image cannot have more than one ':' in the string. Should be used to separate the tag from the image name.")
        # We create object to check against image, that doesn't use docker tags though.
        if ":" in v:
            image_name_only = v.split(":")[0]

        # We search the siteadmintable schema for the images that our tenant is allowed to use.
        all_images = Image.db_get_all(tenant="siteadmintable", site=g.site_id)
        custom_allow_list = []
        main_tenants = ["tacc", "icicleai", "icicle", "dev", "astria", "a2cps", "scoped"]
        for allowed_image in all_images:
            tenants = allowed_image.tenants
            # If "-<tenant>" is present, restrict access for that tenant
            if f"-{g.tenant_id}" in tenants:
                continue
            # "**" allows all tenants
            if "**" in tenants:
                custom_allow_list.append(allowed_image.image)
            # "*" allows only main_tenants
            elif "*" in tenants and g.tenant_id in main_tenants:
                custom_allow_list.append(allowed_image.image)
            # Explicit tenant allow
            elif g.tenant_id in tenants:
                custom_allow_list.append(allowed_image.image)
        # Then we add images from the conf.image_allow_list
        custom_allow_list += conf.get('image_allow_list', [])

        if v.split(':')[0] not in custom_allow_list:
            raise ValueError(f"Custom pod.image images must be in allowlist. View available images at /pods/images or contact admin via github issue or slack (cgarcia). Image derived: {v}")

        return v

    @validator('time_to_stop_default')
    def check_time_to_stop_default(cls, v):
        if not v:
            return v
        if v != -1 and v < 300:
            raise ValueError(f"Pod time_to_stop_default must be -1 or be greater than 300 seconds.")
        return v

    @validator('time_to_stop_instance')
    def check_time_to_stop_instance(cls, v):
        if v and v != -1 and v < 300:
            raise ValueError(f"Pod time_to_stop_instance must be -1 or be greater than 300 seconds.")
        return v

    @validator('networking')
    def check_networking(cls, v):
        if v:
            # Only allow 3 url:port pairs per pod. Trying to keep services minimal.
            # I have uses for 2 ports, not 3, but might as well keep it available.
            if len(v) > 4:
                raise ValueError(f"networking dictionary may only contain up to 4 stanzas")

            # Check keys in networking dict
            # Check key is str, and properly formatted, this should be suffix to urls. "default" means no suffix.
            for env_key, env_val in v.items():
                if not isinstance(env_key, str):
                    raise TypeError(f"networking key must be str. Got type {type(env_key).__name__}.")
                res = re.fullmatch(r'[a-z0-9]+', env_key)
                if not res:
                    raise ValueError(f"networking key must be lowercase alphanumeric. Default is 'default'.")
                if len(env_key) > 64 or len(env_key) < 3:
                    raise ValueError(f"networking key length must be between 3-64 characters. Inputted length: {len(env_key)}")
        return v

    @validator('compute_queue')
    def check_compute_queue(cls, v):
        if v:
            # Ensure compute queue alphanumeric.
            res = re.fullmatch(r'[a-z0-9]+', v)
            if not res:
                raise ValueError(f"compute_queue must be lowercase alphanumeric.")
        return v

VALID_STACK_RESTART_POLICIES = ["ordered", "parallel"]
VALID_READY_CONDITIONS = ["available", "ready"]


class StackMemberDefinition(TemplateTagPodDefinition):
    """One pod within a stack template (kind='stack').

    Inherits every TemplateTagPodDefinition field (image, template, command, environment_variables,
    secret_map, volume_mounts, networking, resources, healthchecks, ...) and adds the stack-topology
    fields. Members are addressed internally by `name`; depends_on and ${stack:<name>:...} references
    use names, never pod_ids. A member carries NO pod_id literal — the external pod_id is chosen at
    instantiation (request pod_ids override, else `{stack_id}{name}`) so the template stays reusable.
    """
    name: str = Field(..., description = "Logical, stack-unique handle for this member. Lowercase "
        "alphanumeric, first char alpha, 3-64 chars. Used by depends_on and ${stack:<name>:...}; this "
        "is NOT the pod_id (that is derived or overridden at instantiation).")
    depends_on: List[str] = Field([], description = "Names of other members that must reach their "
        "ready_condition before this member starts. Rewritten to pod_ids at instantiation.",
        sa_column=Column(ARRAY(String)))
    ready_condition: str = Field("available", description = "When this member counts as 'up' for "
        "dependents: 'available' (status AVAILABLE) or 'ready' (readiness probe passing; requires "
        "healthchecks.readiness).")

    @validator('name')
    def check_name(cls, v):
        # Short names are fine here — the derived pod_id is `{stack_id}{name}`, and stack_id alone
        # already satisfies the pod_id 3-char minimum. So a member may be `db`, `ui`, etc.
        if not re.fullmatch(r'[a-z][a-z0-9]*', v or ""):
            raise ValueError("stack member 'name' must be lowercase alphanumeric, first char alpha.")
        if len(v) > 32 or len(v) < 1:
            raise ValueError(f"stack member 'name' length must be between 1-32 characters. Got: {len(v)}")
        return v

    @validator('ready_condition')
    def check_ready_condition(cls, v):
        if v not in VALID_READY_CONDITIONS:
            raise ValueError(f"ready_condition must be one of {VALID_READY_CONDITIONS}. Got '{v}'.")
        return v


class StackTagDefinition(TapisModel):
    """A whole stack defined in one template tag. Instantiated via POST /pods/stacks/from-template
    into a runtime Stack + member pods."""
    restart_policy: str = Field("ordered", description = "How stack actions order members: 'ordered' "
        "(honor depends_on) or 'parallel'.")
    secret_map: Dict[str, str] = Field({}, description = "Shared, stack-scoped secrets. Members "
        "reference these via ${stack:secrets:KEY}. Only placeholders, ${pods:random:N}, or "
        "${secret:...} are allowed (a shared template must not embed resolved values). Resolved once "
        "per stack; values live on the stack, never copied into member pods.", sa_column=Column(JSON))
    members: List[StackMemberDefinition] = Field([], description = "The pods that make up this stack "
        "(at least one; member names unique).")

    @validator('restart_policy')
    def check_restart_policy(cls, v):
        if v not in VALID_STACK_RESTART_POLICIES:
            raise ValueError(f"restart_policy must be one of {VALID_STACK_RESTART_POLICIES}. Got '{v}'.")
        return v

    @validator('members')
    def check_members(cls, v):
        if not v:
            raise ValueError("stack_definition.members must contain at least one member.")
        names = [m.name for m in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"stack member names must be unique within a stack. Duplicates: {dupes}")
        return v


class TemplateTagBase(TapisApiModel):
    # Required
    template_id: str = Field(..., description="template_id this tag is linked to")#, foreign_key="template.template_id")
    # User Input
    pod_definition: TemplateTagPodDefinition = Field({}, description = "Pod definition for this template (kind='pod').", sa_column=Column(JSON))
    kind: str = Field("pod", description = "Tag kind: 'pod' (single pod_definition, default) or 'stack' (multi-pod stack_definition).")
    stack_definition: Optional[StackTagDefinition] = Field(None, description = "Stack definition when kind='stack'. Instantiate via POST /pods/stacks/from-template.", sa_column=Column(JSON))
    commit_message: str = Field("", description = "Commit message for this template tag.")
    tag: str = Field("latest", description = "Tag for this template. Default is 'latest'.")
    # Optional
    description: str = Field("", description = "Description of template tag.")
    archive_message: str = Field("", description = "If set, metadata message to give users of this template tag.")

class TemplateTagBaseRead(TemplateTagBase):
    # Provided
    tag_timestamp: str = Field("", description = "tag@timestamp for this template tag.", primary_key=True, nullable=False)
    added_by: str = Field("", description = "User who added this template tag.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this template tag was created.")

class TemplateTagBaseFull(TemplateTagBaseRead):
    # Provided
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this template tag.")
    site_id: str = Field("", description = "Tapis site used during creation of this template tag.")

TapisTemplateTagBaseFull = create_model("TapisTemplateTagBaseFull", __base__= type("_ComboModel", (TemplateTagBaseFull, TapisModel), {}))


#### TemplateTag models
class TemplateTag(TapisTemplateTagBaseFull, table=True, validate=True):
    @validator('pod_definition')
    def check_pod_definition(cls, v):
        return v

    @validator('kind')
    def check_kind(cls, v):
        v = v or "pod"
        if v not in ("pod", "stack"):
            raise ValueError(f"kind must be 'pod' or 'stack'. Got '{v}'.")
        return v

    # NOTE: kind/pod_definition/stack_definition mutual-exclusion is validated in the
    # add_template_tag endpoint on the (non-table) NewTemplateTag, NOT here. A table-model
    # model_validator(mode="after") cannot reliably read JSON sa_column fields at construction time
    # (they read empty), which would falsely reject every valid kind='stack' tag.

    @validator('template_id')
    def check_template_id(cls, v):
        # existence check - can be done by foreign key, but it doesn't resolve template.template_id
        template = Template.db_get_with_pk(v, tenant="siteadmintable", site=g.site_id)
        if not template:
            raise ValueError(f"template_id must exist in the database.")
        return v

    @validator('commit_message')
    def check_commit_message(cls, v):
        # ensure commit_message is all ascii
        if not v.isascii():
            raise ValueError(f"commit_message field may only contain ASCII characters.")            
        # make sure commit_message < 255 characters
        if len(v) > 255:
            raise ValueError(f"commit_message field must be less than 255 characters. Inputted length: {len(v)}")
        return v
    
    @validator('description')
    def check_description(cls, v):
        # ensure description is all ascii
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")            
        # make sure description < 400 characters
        if len(v) > 400:
            raise ValueError(f"description field must be less than 400 characters. Inputted length: {len(v)}")
        # I kind of want this to be markdown compatible, for that we should clean to ensure no bad stuff?
        # from bleach import clean
        # v = clean(v, tags=[], attributes={}, protocols=[], strip=True)
        return v

    @validator('archive_message')
    def check_archive_message(cls, v):
        # ensure archive_message is all ascii
        if not v.isascii():
            raise ValueError(f"archive_message field may only contain ASCII characters.")            
        # make sure archive_message < 60 characters
        if len(v) > 60:
            raise ValueError(f"archive_message field must be less than 60 characters. Inputted length: {len(v)}")
        return v

    @validator('tag')
    def check_tag(cls, v):
        # ensure description is lowercase alphanumeric and hyphen
        if not re.match("^[a-zA-Z0-9-.]+$", v):
            raise ValueError(f"tag field may only contain lowercase alphanumeric characters, hyphens, and periods.")
        # make sure description < 80 characters
        if len(v) > 80:
            raise ValueError(f"tag field must be less than 80 characters. Inputted length: {len(v)}")
        return v

    @validator('added_by')
    def check_added_by(cls, v):
        if v:
            return v
        return g.username

    @validator('creation_ts')
    def check_creation_ts(cls, v):
        if v:
            return v
        return datetime.utcnow()

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @model_validator(mode="after")
    def set_tag_timestamp(cls, values):
        creation_ts = getattr(values, 'creation_ts', None)
        tag = getattr(values, 'tag', None)
        if not creation_ts:
            # must wait for creation_ts to be set before we can set tag_timestamp
            return values

        tag_timestamp = f"{tag}@{creation_ts.strftime('%Y-%m-%d-%H:%M:%S')}"
        object.__setattr__(values, "tag_timestamp", tag_timestamp)
        return values

    def display(self, include_configs: bool = False):
        """Return displayable dict, optionally including config content.
        
        Args:
            include_configs: If True, include full config_content in ephemeral mounts.
                           If False (default), replace with placeholder to reduce response size.
        """
        display = self.dict()
        
        # Redact config_content in pod_definition.volume_mounts if not requested (for both ephemeral and tapisvolume types)
        if not include_configs and display.get('pod_definition') and display['pod_definition'].get('volume_mounts'):
            for mount_path, mount_config in display['pod_definition']['volume_mounts'].items():
                if mount_config and mount_config.get('type') in ('ephemeral', 'tapisvolume') and mount_config.get('config_content'):
                    content_size = len(mount_config['config_content'])
                    mount_config['config_content'] = f"<{content_size} bytes - use ?include_configs=true to retrieve>"

        # Kind-aware: a stack tag carries a vestigial default pod_definition (and vice versa) that the
        # mutual-exclusion validator keeps empty. Drop the irrelevant definition so responses and
        # save_as_template snapshots aren't noisy. The combine/instantiation paths read the model
        # fields directly (not display()), so they are unaffected.
        if getattr(self, 'kind', 'pod') == 'stack':
            display.pop('pod_definition', None)
        else:
            display.pop('stack_definition', None)

        return display
    
    def display_small(self):
        display = self.dict()
        display.pop('pod_definition', None)
        display.pop('stack_definition', None)
        display.pop('template_id')
        return display


class TemplateTagDependents(TapisApiModel):
    """Dependency information for a template tag."""
    dependant_pods: List[str] = Field([], description="List of pod IDs that depend on this template tag.")
    dependant_pod_count: int = Field(0, description="Number of pods that depend on this template tag.")
    dependant_tags: List[str] = Field([], description="List of template tag timestamps that depend on this template tag.")
    dependant_tags_count: int = Field(0, description="Number of template tags that depend on this template tag.")


class TemplateTagWithDependents(TemplateTagBaseRead):
    """Template tag with optional dependency information."""
    # Include all fields from TemplateTagBaseFull
    tenant_id: str = Field("", description="Tapis tenant used during creation of this template tag.")
    site_id: str = Field("", description="Tapis site used during creation of this template tag.")
    # Optional dependents field
    dependents: Optional[TemplateTagDependents] = Field(None, description="Dependency information (only present when include_dependencies=true).")

    class Config:
        extra = "allow"


class TemplateTagNoDefinition(TapisApiModel):
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this template tag was created.")
    added_by: str = Field("", description = "User who added this template tag.")
    commit_message: str = Field("", description = "Commit message for this template tag.")
    tag: str = Field("latest", description = "Tag for this template. Default is 'latest'.")
    tag_timestamp: str = Field("", description = "tag@timestamp for this template tag.")


class NewTemplateTag(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Template class.
    """
    pod_definition: TemplateTagPodDefinition = Field({}, description = "Pod definition for this template tag (kind='pod').", sa_column=Column(JSON))
    kind: str = Field("pod", description = "Tag kind: 'pod' (default) or 'stack'.")
    stack_definition: Optional[StackTagDefinition] = Field(None, description = "Stack definition for this template tag (required when kind='stack').", sa_column=Column(JSON))
    commit_message: str = Field(..., description = "Commit message for this template tag.")
    tag: str = Field("latest", description = "Tag for this template. Default is 'latest'.")

class NewTemplateTagFromPod(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Template class when creating a new template tag from a pod.
    """
    commit_message: str = Field(..., description = "Commit message for this template tag.")
    tag: str = Field("latest", description = "Tag for this template. Default is 'latest'.")
    template_id: str = Field(..., description="template_id this tag is linked to")

class TemplateTagResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: TemplateTag
    status: str
    version: str


class TemplateTagDeleteResponse(TapisApiModel):
    """Response type for delete operations that may return None result on error."""
    message: str
    metadata: Dict
    result: Optional[Dict] = None
    status: str
    version: str


class TemplateTagsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[TemplateTag]
    status: str
    version: str

class TemplateTagsSmallResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[TemplateTagNoDefinition]
    status: str
    version: str


class TemplateTagsWithDependentsResponse(TapisApiModel):
    """Response type for template tags list with optional dependency information."""
    message: str
    metadata: Dict
    result: List[TemplateTagWithDependents]
    status: str
    version: str


class TemplateTagWithDependentsResponse(TapisApiModel):
    """Response type for a single template tag with optional dependency information."""
    message: str
    metadata: Dict
    result: TemplateTagWithDependents
    status: str
    version: str

from codes import ERROR, SPAWNER_SETUP, CREATING, \
    REQUESTED, DELETING
from models_templates_tags import TemplateTag, TemplateTagPodDefinition, derive_template_info
from kubernetes_utils import create_pod, create_service, create_pvc, KubernetesError
from kubernetes import client, config

import re

from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
from __init__ import t
logger = get_logger(__name__)


def get_modified_template_fields(original_template, modified_template_def):
    """
    Returns a dictionary of fields that have been modified from a base template
    Meaning, returns fields that user defined in template.
    """
    changed_fields = {}
    for key, value in original_template.items():
        if key not in modified_template_def or value != modified_template_def[key]:
            changed_fields[key] = modified_template_def[key]
    if changed_fields.get('resources'):
        ### resources.gpus, resources.mem_Limit, etc exists.
        # Only return resources in dict if subfield not null, so we delete null subfields
        for resource_key, resource_val in changed_fields['resources'].copy().items():
            if resource_val is None:
                del changed_fields['resources'][resource_key]
    return changed_fields

def combine_pod_and_template_recursively(input_obj, template_name, seen_templates=None, tenant: str = None, site: str = None):
    """
    --- run with
    pod = Pod.db_get_with_pk(pk_id='testingfastapi', tenant='dev', site='tacc')
    d = combine_pod_and_template_recursively(pod, "template21:car@2024-06-11-18:09:39")
    d.description
    """
    logger.debug(f"Top of combine_pod_and_template_recursively for template: {template_name}, tenant: {tenant}, site: {site}")
    if seen_templates is None:
        seen_templates = set()

    if template_name:
        if template_name in seen_templates:
            raise ValueError(f"Infinite loop detected: template {template_name} is referenced more than once in template waterfall.")
        seen_templates.add(template_name)

        template_name_str, template, template_tag = derive_template_info(template_name, tenant=tenant, site=site)
        modified_fields = get_modified_template_fields(TemplateTagPodDefinition().dict(), template_tag.pod_definition)

        # First, recursively combine the input_obj with the next template in the chain
        input_obj = combine_pod_and_template_recursively(input_obj, modified_fields.get('template'), seen_templates, tenant, site)

        # Then, apply the current template to the input_obj
        try:
            logger.debug("Attempting to combine pod and template recursively22")
            for mod_key, mod_val in modified_fields.items():
                logger.debug(f"mod_key: {mod_key}; mod_val: {mod_val}")
                if mod_key == "resources":
                    # Merge template resources with pod resources, pod values take precedence
                    pod_resources = getattr(input_obj, "resources", {})
                    if hasattr(pod_resources, 'dict'):
                        pod_resources = pod_resources.dict()
                    template_resources = template_tag.pod_definition[mod_key]
                    merged_resources = template_resources.copy() if template_resources else {}
                    merged_resources.update(pod_resources or {})
                    setattr(input_obj, mod_key, merged_resources)
                elif mod_key.startswith("resources."):
                    logger.critical('hey')
                    outer_arg, inner_arg = resources.split('.') # resources.gpus
                    outer_obj = getattr(input_obj, outer_arg) # resources
                    logger.critical('oh no!')
                    new_obj_value = template_tag.pod_definition[outer_arg][inner_arg]
                    setattr(outer_obj, inner_arg, new_obj_value)
                elif mod_key == "networking":
                    # must take template3, update with template2, template,1 and then pod, in that order
                    # Preserving order of objs, pod being the most important.
                    # Merge template networking with pod networking, pod values take precedence
                    final_network_obj = getattr(input_obj, mod_key)
                    template_networks = template_tag.pod_definition[mod_key]
                    for network_name, network_def in template_networks.items():
                        # Start with template's network definition
                        merged_network = network_def.copy()
                        # If pod has this network, update with pod's values
                        if network_name in final_network_obj:
                            merged_network.update(final_network_obj[network_name])
                        # Get tenant_id from input_obj or use default from context
                        tenant_id = getattr(input_obj, 'tenant_id')
                        # Fetch base_url from tenant_cache
                        logger.debug(f"Fetching base_url for pod {input_obj.pod_id} network {network_name}")
                        base_url = t.tenant_cache.get_tenant_config(tenant_id=tenant_id).base_url
                        # Generate URL based on network name
                        if network_name == 'default':
                            url = base_url.replace("https://", f"{input_obj.pod_id}.pods.")
                        else:
                            url = base_url.replace("https://", f"{input_obj.pod_id}-{network_name}.pods.")
                        # Set the URL in network definition
                        merged_network['url'] = url
                        final_network_obj[network_name] = merged_network
                    setattr(input_obj, mod_key, final_network_obj)
                elif mod_key == "environment_variables":
                    logger.debug(f"environment_variables----")
                    # Self-documenting method of either overwriting alls envs or appending to them
                    if input_obj.environment_variables.get("_TAPIS_INTERNAL_USE_TEMPLATE_ENVS", True):
                        # inputobj and templateobj envs are dicts. If using template vars we use those as base and write input over
                        input_envs = input_obj.environment_variables
                        final_envs = template_tag.pod_definition[mod_key]
                        final_envs.update(input_envs)
                        setattr(input_obj, mod_key, final_envs)
                        logger.debug(f"_TAPIS_INTERNAL_USE_TEMPLATE_ENVS is True - input_obj.environment_variables: {input_obj.environment_variables}")
                    else:
                        # We're not using templateobj envs, so we only use inputobj envs
                        logger.debug(f"_TAPIS_INTERNAL_USE_TEMPLATE_ENVS is False - input_obj.environment_variables: {input_obj.environment_variables}")
                elif mod_key.startswith("volume_mount."):
                    print('dog')
                elif mod_key == "volume_mounts":
                    # Use only user's volume_mounts by default, or merge if _TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES is True
                    pod_volume_mounts = getattr(input_obj, "volume_mounts", {})
                    if hasattr(pod_volume_mounts, 'dict'):
                        pod_volume_mounts = pod_volume_mounts.dict()
                    template_volume_mounts = template_tag.pod_definition[mod_key]
                    env_vars = getattr(input_obj, "environment_variables", {})
                    use_template_vols = env_vars.get('_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES', True)
                    if use_template_vols:
                        merged_volume_mounts = template_volume_mounts.copy() if template_volume_mounts else {}
                        merged_volume_mounts.update(pod_volume_mounts or {})
                        setattr(input_obj, mod_key, merged_volume_mounts)
                    else:
                        setattr(input_obj, mod_key, pod_volume_mounts or {})
                elif mod_key.startswith("template"):
                    pass ## Don't need this one
                elif mod_key in input_obj.modified_fields:
                    pass ## Don't modify user-modified fields, sans the above as they're dict updates and not overwrites
                else:
                    setattr(input_obj, mod_key, mod_val)

            logger.debug(f"End of combine_pod_and_template_recursively for template: {template_name}, tenant: {tenant}, site: {site}")
            try:
                if input_obj.resources and not type(input_obj.resources) == dict:
                    input_obj.resources = input_obj.resources.dict()
            except Exception as e:
                logger.debug(f'this resources part: Got exception when attempting to combine pod and templates: {e}')
                pass

            try:
                if input_obj.networking and not type(input_obj.networking) == dict:
                    input_obj.networking = input_obj.networking.dict()
            except Exception as e:
                logger.debug(f'this networking part: Got exception when attempting to combine pod and templates: {e}')
                pass

        except Exception as e:
            logger.debug(f'Got exception when attempting to combine pod and templates: {e}')

    return input_obj
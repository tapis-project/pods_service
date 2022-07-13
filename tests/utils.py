# Utilities shared across testing modules.
import json
import os
import pytest
import requests
import time
import subprocess

from tapipy.tapis import Tapis
from tapisservice.tenants import TenantCache
from tapisservice.logs import get_logger
from tapisservice.config import conf
logger = get_logger(__name__)

# Need base_url as it's where we direct calls. But also need SK url for tapipy.
base_url = os.environ.get('base_url', 'http://172.17.0.1:8000')
case = os.environ.get('case', 'snake')
testuser_tenant = os.environ.get('tenant', 'dev')


def get_service_tapis_client():
    sk_url = os.environ.get('sk_url', conf.primary_site_admin_tenant_base_url)
    tenant_id = os.environ.get('tenant', 'admin')
    service_password = os.environ.get('service_password', conf.service_password)
    jwt = os.environ.get('jwt', None)
    resource_set = os.environ.get('resource_set', 'local')
    custom_spec_dict = os.environ.get('custom_spec_dict', None)
    download_latest_specs = os.environ.get('download_latest_specs', False)
    # if there is no tenant_id, use the service_tenant_id and primary_site_admin_tenant_base_url configured for the service:    
    t = Tapis(base_url=sk_url or base_url,
              tenant_id=tenant_id,
              username='abaco',
              account_type='service',
              service_password=service_password,
              jwt=jwt,
              resource_set=resource_set,
              custom_spec_dict=custom_spec_dict,
              download_latest_specs=download_latest_specs,
              is_tapis_service=True,
              plugins=["tapisservice"],
              tenants=TenantCache(),
              _tapis_set_x_headers_from_service=True)
    if not jwt:
        logger.debug("tapis service client constructed, now getting tokens.")
        t.get_tokens()
    return t

t = get_service_tapis_client()

# In dev:
# service account owns abaco_admin and abaco_privileged roles
# _abaco_testuser_admin is granted abaco_admin role
# _abaco_testuser_privileged is granted abaco_privileged role
# _abaco_testuser_regular is granted nothing
# @pytest.fixture(scope='session', autouse=True)
# def create_test_roles():
#     # Using Tapipy to ensure each abaco environment has proper roles and testusers created before starting
#     all_role_names = t.sk.getRoleNames(tenant=testuser_tenant, _tapis_set_x_headers_from_service=True)
#     if not 'abaco_admin' in all_role_names.names:
#         print('Creating role: abaco_admin')
#         t.sk.createRole(roleTenant=testuser_tenant, roleName='abaco_admin', description='Admin role in Abaco.', _tapis_set_x_headers_from_service=True)
#     if not 'abaco_privileged' in all_role_names.names:
#         print('Creating role: abaco_privileged')
#         t.sk.createRole(roleTenant=testuser_tenant, roleName='abaco_privileged', description='Privileged role in Abaco.', _tapis_set_x_headers_from_service=True)
#     t.sk.grantRole(tenant=testuser_tenant, user='_abaco_testuser_admin', roleName='abaco_admin', _tapis_set_x_headers_from_service=True)
#     t.sk.grantRole(tenant=testuser_tenant, user='_abaco_testuser_privileged', roleName='abaco_privileged', _tapis_set_x_headers_from_service=True)

@pytest.fixture(scope='session', autouse=True)
def headers():
    return get_tapis_token_headers('_pods_testuser_admin', None)

@pytest.fixture(scope='session', autouse=True)
def privileged_headers():
    return get_tapis_token_headers('_pods_testuser_privileged', None)

@pytest.fixture(scope='session', autouse=True)
def regular_headers():
    return get_tapis_token_headers('_pods_testuser_regular', None)

@pytest.fixture(scope='session', autouse=True)
def limited_headers():
    return get_tapis_token_headers('_pods_testuser_limited', None)

@pytest.fixture(scope='session', autouse=True)
def alternative_tenant_headers():
    # Find an alternative tenant than the one currently being tested, usually
    # "dev", if "dev" is used, "tacc" will be used. Or otherwise specified.
    alt_tenant = 'dev'
    alt_alt_tenant = 'tacc'
    curr_tenant = get_tenant()
    if curr_tenant == alt_tenant:
        alt_tenant = alt_alt_tenant
    return get_tapis_token_headers('_pods_testuser_regular', alt_tenant)

@pytest.fixture(scope='session', autouse=True)
def cycling_headers(regular_headers, privileged_headers):
    return {'regular': regular_headers,
            'privileged': privileged_headers}

def get_tapis_token_headers(user, alt_tenant=None):
    # Uses alternative tenant if provided.
    token_res = t.tokens.create_token(account_type='user', 
                                      token_tenant_id=alt_tenant or testuser_tenant,
                                      token_username=user,
                                      access_token_ttl=999999,
                                      generate_refresh_token=False,
                                      use_basic_auth=False,
                                      _tapis_set_x_headers_from_service=True)
    if not token_res.access_token or not token_res.access_token.access_token:
        raise KeyError(f"Did not get access token; token response: {token_res}")
    header_dat = {"X-Tapis-Token": token_res.access_token.access_token}
    return header_dat

@pytest.fixture(scope='session', autouse=True)
def wait_for_rabbit():
    rabbitmq_dash_host = conf.rabbitmq_dash_host

    fn_call = f'/home/tapis/rabbitmqadmin -H {rabbitmq_dash_host} '

    # Get admin credentials from rabbit_uri. Add auth to fn_call if it exists.

    admin_user = conf.rabbitmq_user or "guest"
    admin_pass = conf.rabbitmq_pass or "guest"

    fn_call += (f'-u {admin_user} ')
    fn_call += (f'-p {admin_pass} ')

    # We poll to check rabbitmq is operational. Done by trying to list vhosts, arbitrary command.
    # Exit code 0 means rabbitmq is running. Need access to rabbitmq dash/management panel.
    i = 8
    while i:
        result = subprocess.run(fn_call + f'list vhosts', shell=True)
        if result.returncode == 0:
            break
        else:
            time.sleep(2)
        i -= 1
    time.sleep(7)

def get_tenant():
    """ Get the tenant_id associated with the test suite requests."""
    return t.tenant_id

def get_jwt_headers(file_path='/home/tapis/tests/jwt-abaco_admin'):
    with open(file_path, 'r') as f:
        jwt_default = f.read()
    jwt = os.environ.get('jwt', jwt_default)
    if jwt:
        jwt_header = os.environ.get('jwt_header', 'X-Jwt-Assertion-DEV-DEVELOP')
        headers = {jwt_header: jwt}
    else:
        token = os.environ.get('token', '')
        headers = {'Authorization': f'Bearer {token}'}
    return headers

def delete_pods(client, headers):
    rsp = client.get("/pods",
                     headers=headers)
    result = basic_response_checks(rsp)
    for pod in result:
        url = f'/pods/{pod.get("pod_id")}'
        rsp = client.delete(url, headers=headers)
        basic_response_checks(rsp)

def response_format(rsp):
    assert 'application/json' in rsp.headers['content-type']
    data = json.loads(rsp.content.decode('utf-8'))
    assert 'message' in data.keys()
    assert 'status' in data.keys()
    assert 'version' in data.keys()
    return data

def basic_response_checks(rsp):
    if not rsp.status_code in [200, 201]:
        print(rsp.content)
    assert rsp.status_code in [200, 201]
    response_format(rsp)
    data = json.loads(rsp.content.decode('utf-8'))
    assert 'result' in data.keys()
    result = data['result']
    print(result)
    return result

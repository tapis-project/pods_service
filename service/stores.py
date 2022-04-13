from functools import partial
import os

from store import NeoStore

from tapisservice.config import conf
from __init__ import t
import subprocess
import re
import requests as r
import time

from tapisservice.logs import get_logger
logger = get_logger(__name__)

# Figure out which sites this deployment has to manage.
# Getting current site and checking if we're primary.
SITE_LIST = []
tenant_object = t.tenant_cache.get_tenant_config(tenant_id=t.tenant_id)
if tenant_object.site.primary:
    for site_object in t.tenants.list_sites(_tapis_set_x_headers_from_service=True):
        SITE_LIST.append(site_object.site_id)
else:
    SITE_LIST.append(tenant_object.site_id)

def get_site_rabbitmq_uri(site):
    """
    Takes site and gets site specific rabbit uri using correct
    site vhost and site's authentication from config.
    """
    logger.debug(f'Top of get_site_rabbitmq_uri. Using site: {site}')
    # Get rabbit_uri and check if it's the proper format. "amqp://rabbit:5672"
    # auth information should NOT be included on conf.
    rabbit_uri = conf.rabbit_uri
    if "@" in rabbit_uri:
        raise KeyError("rabbit_uri in config.json should no longer have auth. Configure with config.")

    # Getting site object with parameters for specific site.
    site_object = conf.get(f'{site}_site_object') or {}

    # Checking for RabbitMQ credentials for each site.
    site_rabbitmq_user = f"kg_{site}_user"
    site_rabbitmq_pass = site_object.get('site_rabbitmq_pass') or conf.get("global_site_object").get("site_rabbitmq_pass")

    # Setting up auth string.
    if site_rabbitmq_user and site_rabbitmq_pass:
        auth = f"{site_rabbitmq_user}:{site_rabbitmq_pass}"
    else:
        auth = site_rabbitmq_user
    
    # Adding auth to rabbit_uri.
    rabbit_uri = rabbit_uri.replace("amqp://", f"amqp://{auth}@")
    # Adding site vhost to rabbit_uri
    rabbit_uri += f"/kg_{site}"
    return rabbit_uri


def rabbit_initialization():
    """
    Initial site initialization for RabbitMQ using the RabbitMQ utility.
    Consists of creating a user/pass and vhost for each site. Each site's user
    gets permissions to it's vhosts. Primary site gets control to each vhost.
    One-time/deployment
    """
    try:
        rabbit_dash_host = conf.rabbit_dash_host

        # Creating the subprocess call (Has to be str, list not working due to docker
        # aliasing of rabbit with it's IP address (Could work? But this works too.).).
        fn_call = f'/home/tapis/rabbitmqadmin -H {rabbit_dash_host} '

        # Get admin credentials from rabbit_uri. Add auth to fn_call.
        # Note: If admin password not set in rabbit env with compose the user and pass
        # or just the pass (if only user is set) will default to "guest".
        admin_rabbitmq_user = conf.get("rabbit_user", "guest")
        admin_rabbitmq_pass = conf.get("rabbit_pass", "guest")

        if not isinstance(admin_rabbitmq_user, str) or not isinstance(admin_rabbitmq_pass, str):
            msg = f"RabbitMQ creds must be of type 'str'. user: {type(admin_rabbitmq_user)}, pass: {type(admin_rabbitmq_pass)}."
            logger.critical(msg)
            raise RuntimeError(msg)

        if not admin_rabbitmq_user or not admin_rabbitmq_pass:
            msg = f"RabbitMQ creds were given, but were None or empty. user: {admin_rabbitmq_user}, pass: {admin_rabbitmq_pass}" 
            logger.critical(msg)
            raise RuntimeError(msg)

        if admin_rabbitmq_user == "guest" or admin_rabbitmq_pass == "guest":
            logger.warning(f"RabbitMQ using default admin information. Not secure.")
        logger.debug(f"Administrating RabbitMQ with user: {admin_rabbitmq_user} and pass: ***.")

        fn_call += (f'-u {admin_rabbitmq_user} ')
        fn_call += (f'-p {admin_rabbitmq_pass} ')

        # We poll to check rabbitmq is operational. Done by trying to list vhosts, arbitrary command.
        # Exit code 0 means rabbitmq is running. Need access to rabbitmq dash/management panel.
        i = 15
        logger.critical(fn_call)
        while i:
            result = subprocess.run(fn_call + f'list vhosts', shell=True, capture_output=True)
            if result.returncode == 0:
                break
            elif result.stderr:
                rabbit_error = result.stderr.decode('UTF-8')
                if "Errno 111" in rabbit_error:
                    msg = "Rabbit still initializing."
                    logger.debug(msg)
                elif "Access refused" in rabbit_error:
                    msg = "Rabbit admin user or pass misconfigured."
                    logger.critical(msg)
                    raise RuntimeError(msg)
                else:
                    msg = f"RabbitMQ has thrown an error. e: {rabbit_error}"
                    logger.critical(msg)
                    raise RuntimeError(msg)
            else:
                msg = "Rabbit still initializing."
                logger.debug(msg)
            time.sleep(2)
            i -= 1
        if not result.returncode == 0:
            msg = "Timeout waiting for RabbitMQ to start."
            logger.critical(msg)
            raise RuntimeError(msg)
    except Exception as e:
        msg = f"Error during RabbitMQ start process. e: {e}"
        logger.critical(msg)
        raise Exception(msg)
    
    # Creating user/pass, vhost, and assigning permissions for rabbitmq.
    try:
        for site in SITE_LIST:
            # Getting site object with parameters for specific site.
            site_object = conf.get(f'{site}_site_object') or {}

            # Checking for RabbitMQ credentials for each site.
            site_rabbitmq_user = f"kg_{site}_user"
            site_rabbitmq_pass = site_object.get('site_rabbitmq_pass') or ""
            if not site_rabbitmq_pass:
                msg = f'No site_rabbitmq_pass found for site: {site}. Using Global.'
                logger.warning(msg)
                site_rabbitmq_pass = conf.global_site_object.get("site_rabbitmq_pass")
                if not site_rabbitmq_pass:
                    msg = f'No global "site_rabbitmq_pass" to act as default password. Cannot initialize.'
                    logger.critical(msg)
                    raise KeyError(msg)

            # Site DB Name
            site_db_name = f"kg_{site}"

            # Initializing site user account.
            subprocess.run(fn_call + f'declare user name={site_rabbitmq_user} password={site_rabbitmq_pass} tags=None', shell=True) # create user/pass

            # Creating site vhost. Granting permissions to site user and admin.
            logger.debug(f"Creating vhost named '{site_db_name}' for site - {site}. {site_rabbitmq_user} and {admin_rabbitmq_user} users are being granted read/write.")
            subprocess.run(fn_call + f'declare vhost name={site_db_name}', shell=True) # create vhost
            subprocess.run(fn_call + f'declare permission vhost={site_db_name} user={site_rabbitmq_user} configure=.* write=.* read=.*', shell=True) # site user perm
            subprocess.run(fn_call + f'declare permission vhost={site_db_name} user={admin_rabbitmq_user} configure=.* write=.* read=.*', shell=True) # admin perm
            logger.debug(f"RabbitMQ init complete for site: {site}.")
            print(f"RabbitMQ init complete for site: {site}.")
    except Exception as e:
        msg = f"Error setting up RabbitMQ for site: {site} e: {repr(e)}"
        logger.critical(msg)
        raise Exception(msg)


def neo_initialization():
    """
    Initial setup for neo.
    Database configuration information has to be provided by config-local. The
    config can give multiple databases, this is so that we can give each site
    (even tenant if we want) it's own backend.
    Config'ed user is already root in it's database.
    """
    # 
    # Getting user/pass of database.
    print("This isn't needed")
    # try:
    #     admin_neo_user = conf.admin_neo_user
    #     admin_neo_pass = conf.
        
    # except Exception as e:
    #     msg = f""
    #     logger.critical(msg)
    #     raise RuntimeError(msg)

    # # We initialize each database requested.
    # for database in conf.databases:


# def neo_index_initialization():
#     """
#     Seperate function to initialize mongo so that he sometimes lengthy process
#     doesn't slow down stores.py imports as it only needs to be called once.
#     Initialization consists of creating indexes for Mongo. One-time/deployment
#     """
#     for site in SITE_LIST:
#         # Getting site object with parameters for specific site.
#         site_object = conf.get(f"{site}_site_object") or {}

#         # Sets an expiry variable 'exp'. So whenever a document gets placed with it
#         # the doc expires after 0 seconds. BUT! If exp is set as a Mongo Date, the
#         # Mongo TTL index will wait until that Date and then delete after 0 seconds.
#         # So we can delete at a specific time if we set expireAfterSeconds to 0
#         # and set the time to expire on the 'exp' variable.
#         try:
#             logs_store[site]._db.create_index("exp", expireAfterSeconds=0)
#         except errors.OperationFailure:
#             # this will happen if the index already exists.
#             pass

#         # Creating wildcard text indexing for full-text mongo search
#         logs_store[site].create_index([('$**', TEXT)])
#         executions_store[site].create_index([('$**', TEXT)])
#         actors_store[site].create_index([('$**', TEXT)])
#         workers_store[site].create_index([('$**', TEXT)])

def role_initialization():
    """
    Creating roles at reg startup so that we can insure they always exist and that they're in all tenants from now on.
    """
    tenants = conf.tenants or []
    # for tenant in tenants:
    #     t.sk.createRole(roleTenant=tenant, roleName='abaco_admin', description='Admin role in Abaco.', _tapis_set_x_headers_from_service=True)
    #     t.sk.createRole(roleTenant=tenant, roleName='abaco_privileged', description='Privileged role in Abaco.', _tapis_set_x_headers_from_service=True)
    #     t.sk.grantRole(tenant=tenant, roleName='abaco_admin', user='abaco', _tapis_set_x_headers_from_service=True)
    #     t.sk.grantRole(tenant=tenant, roleName='abaco_admin', user='streams', _tapis_set_x_headers_from_service=True)


if __name__ == "__main__":
    # rabbit and neo only go through init on primary site.
    rabbit_initialization()
    #neo_initialization()
    role_initialization()

# We do this outside of a function because the 'store' objects need to be imported
# by other scripts. Functionalizing it would create more code and make it harder
# to read in my opinion.
neo_store = {}

for db_name, db_conf in conf.databases.items():
    neo_store[db_name] = NeoStore(host=db_conf["host"],
                                  port=db_conf["bolt"],
                                  user=db_conf["user"],
                                  passw=db_conf["pass"])

for site in SITE_LIST:
    if site not in neo_store.keys():
        logger.warning(f"No neo config for site `{site}`. Don't attempt to use.")
import os
import time
import subprocess
from store import PostgresStore
from __init__ import t
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

# Get all sites and tenants for all sites.
SITE_TENANT_DICT = {} # {site_id1: [tenant1, tenant2, ...], site_id2: ...}
for tenant in t.tenant_cache.tenants.values():
    if not SITE_TENANT_DICT.get(tenant.site_id):
        SITE_TENANT_DICT[tenant.site_id] = []
    SITE_TENANT_DICT[tenant.site_id].append(tenant.tenant_id)
curr_tenant_obj = t.tenant_cache.get_tenant_config(tenant_id=t.tenant_id)
# Delete excess sites when current site is not primary. Non-primary sites will never have to manage other sites.
if not curr_tenant_obj.site.primary:
    SITE_TENANT_DICT = {curr_tenant_obj.site: SITE_TENANT_DICT[curr_tenant_obj.site]}


def get_site_rabbitmq_uri(site):
    """
    Takes site and gets site specific rabbitmq uri using correct
    site vhost and site's authentication from config.
    """
    logger.debug(f'Top of get_site_rabbitmq_uri. Using site: {site}')
    # Get rabbitmq_uri and check if it's the proper format. "amqp://rabbitmq:5672"
    # auth information should NOT be included on conf.
    rabbitmq_uri = conf.rabbitmq_uri
    if "@" in rabbitmq_uri:
        raise KeyError("rabbit_uri in config.json should no longer have auth. Configure with config.")

    # Getting site object with parameters for specific site.
    site_object = conf.get(f'{site}_site_object') or {}

    # Checking for RabbitMQ credentials for each site.
    site_rabbitmq_user = f"pods_{site}_user"
    site_rabbitmq_pass = site_object.get('site_rabbitmq_pass') or conf.get("global_site_object").get("site_rabbitmq_pass")

    # Setting up auth string.
    if site_rabbitmq_user and site_rabbitmq_pass:
        auth = f"{site_rabbitmq_user}:{site_rabbitmq_pass}"
    else:
        auth = site_rabbitmq_user
    
    # Adding auth to rabbitmq_uri.
    rabbitmq_uri = rabbitmq_uri.replace("amqp://", f"amqp://{auth}@")
    # Adding site vhost to rabbitmq_uri
    rabbitmq_uri += f"/pods_{site}"
    return rabbitmq_uri


def rabbitmq_init():
    """
    Initial site init for RabbitMQ using the RabbitMQ utility.
    Consists of creating a user/pass and vhost for each site. Each site's user
    gets permissions to it's vhosts. Primary site gets control to each vhost.
    One-time/deployment
    """
    try:
        rabbitmq_dash_host = conf.rabbitmq_dash_host

        # Creating the subprocess call (Has to be str, list not working due to docker
        # aliasing of rabbitmq with it's IP address (Could work? But this works too.).).
        fn_call = f'/home/tapis/rabbitmqadmin -H {rabbitmq_dash_host} '

        # Get admin credentials from rabbit_uri. Add auth to fn_call.
        # Note: If admin password not set in rabbit env with compose the user and pass
        # or just the pass (if only user is set) will default to "guest".
        admin_rabbitmq_user = conf.get("rabbitmq_user", "guest")
        admin_rabbitmq_pass = conf.get("rabbitmq_pass", "guest")

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
        while i:
            result = subprocess.run(fn_call + f'list vhosts', shell=True, capture_output=True)
            if result.returncode == 0:
                break
            elif result.stderr:
                rabbitmq_error = result.stderr.decode('UTF-8')
                if "Errno 111" in rabbitmq_error:
                    msg = f"RabbitMQ still initializing. {i} tries remaining. Sleeping 2 seconds."
                    logger.debug(msg)
                elif "Access refused" in rabbitmq_error:
                    msg = "Rabbit admin user or pass misconfigured."
                    logger.critical(msg)
                    raise RuntimeError(msg)
                else:
                    msg = f"RabbitMQ has thrown an error. e: {rabbitmq_error}"
                    logger.critical(msg)
                    raise RuntimeError(msg)
            else:
                msg = f"RabbitMQ still initializing. {i} tries remaining. Sleeping 2 seconds."
                logger.debug(msg)
            time.sleep(2)
            i -= 1
        if not result.returncode == 0:
            msg = "Timeout waiting for RabbitMQ to start."
            logger.critical(msg)
            raise RuntimeError(msg)
    except Exception as e:
        msg = f"Error during RabbitMQ start process. e: {repr(e)}"
        logger.critical(msg)
        # Can't 100% remember unfortunately. I believe rabbitmq exposes password when it crashes.
        # This covers that with our logging. The crash raises to the user's API response, hence the neccessary cover up. 
        e.args = [msg]
        raise e
    
    # Creating user/pass, vhost, and assigning permissions for rabbitmq.
    try:
        for site in SITE_TENANT_DICT.keys():
            # Getting site object with parameters for specific site.
            site_object = conf.get(f'{site}_site_object') or {}

            # Checking for RabbitMQ credentials for each site.
            site_rabbitmq_user = f"pods_{site}_user"
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
            site_db_name = f"pods_{site}"

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
        msg = f"Error setting up RabbitMQ for site: {site}. e: {repr(e)}"
        logger.critical(msg)
        # Can't 100% remember unfortunately. I believe rabbitmq exposes password when it crashes.
        # This covers that with our logging. The crash raises to the user's API response, hence the neccessary cover up. 
        e.args = [msg]
        raise e


def role_init():
    """
    Creating roles at reg startup so that we can insure they always exist and that they're in all tenants from now on.
    """
    # for tenant in tenants:
    #     t.sk.createRole(roleTenant=tenant, roleName='abaco_admin', description='Admin role in Abaco.', _tapis_set_x_headers_from_service=True)
    #     t.sk.createRole(roleTenant=tenant, roleName='abaco_privileged', description='Privileged role in Abaco.', _tapis_set_x_headers_from_service=True)
    #     t.sk.grantRole(tenant=tenant, roleName='abaco_admin', user='abaco', _tapis_set_x_headers_from_service=True)
    #     t.sk.grantRole(tenant=tenant, roleName='abaco_admin', user='streams', _tapis_set_x_headers_from_service=True)


def create_pg_objects():
    admin_postgres_user = conf.postgres_user
    admin_postgres_pass = conf.postgres_pass

    # Create default pg object to use later (creating dbs, etc)
    pg_default = PostgresStore(username=admin_postgres_user,
                               password=admin_postgres_pass,
                               host=conf.postgres_host,
                               dbname="postgres")

    pg_store = {}
    
    # Now add in all of the sites and tenant pg engines.
    for site, tenants in SITE_TENANT_DICT.items():
        tenant_copy = tenants.copy() + ["siteadmintables", "defaulttables"]
        for tenant in tenant_copy:
            pg = PostgresStore(username=admin_postgres_user,
                               password=admin_postgres_pass,
                               host=conf.postgres_host,
                               dbname=site,
                               dbschema=tenant)
            if not pg_store.get(site):
                pg_store[site] = {}
            pg_store[site][tenant] = pg
    return pg_store, pg_default

# We do this outside of a function because the 'store' objects need to be imported
# by other scripts. Functionalizing it would create more code and make it harder
# to read in my opinion.
try:
    pg_store, pg_default = create_pg_objects()
except Exception as e:
    logger.critical(e)
    raise


if __name__ == "__main__":
    # rabbitmq and postgres only go through init on primary site.
    rabbitmq_init()
    role_init()
    import subprocess
    time.sleep(3)
    print("\n\n\n\n\nAlembic -- Updating with current migration files")
    subprocess.run("alembic upgrade head", shell=True)
    print("\n\n\n\n\nAlembic -- Running revision autogeneration")
    subprocess.run("alembic revision -m 'init5' --autogenerate", shell=True) #Dev step for new migrations
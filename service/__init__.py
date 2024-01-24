from tapisservice.tenants import TenantCache
from tapisservice.auth import get_service_tapis_client
from tapisservice.logs import get_logger
from tapipy.tapis import TapisResult
logger = get_logger(__name__)

Tenants = TenantCache()

# Create Tapis `t` client
for _ in range(10):
    try:
        t = get_service_tapis_client(tenants=Tenants)
        break  # Exit the loop if t is successfully created
    except Exception as e:
        logger.error(f'Could not instantiate tapy service client. Exception: {e}')
        time.sleep(2)  # Delay for 2 seconds before the next attempt
else:
    msg = 'Failed to create tapy service client after 10 attempts (20 seconds), networking?'
    logger.error(msg)
    raise RuntimeError(msg)
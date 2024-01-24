# Change Log
All notable changes to this project will be documented in this file.

## 1.5.3 - 2023-12-01
- `1.5.1` and `1.5.2`: No Changes. Jumping to `1.5.3` to match deployer version.

### Breaking Changes:
- Implemented direct access to NFS server instead of routing through Files for volumes/snapshots.
    - This solves occassional networking hiccups causing troubles when Files couldn't be accessed.
    - This solves deployment across multiple namespaces as Files access was a stickler.
- Health is now split into health and health-central.
    - `health-central` deploys with the main stack.
        - It takes care of metrics, traefik management, and NFS management.
    - `health` deploys with computer (health and spawner) in whatever namespace.
        - Takes care of Kubernetes health and management in a particular namespace.
- New deployment files for the above features along with deleting no longer used files.
    - Works locally as well.
- Fix in traefik to properly throw a 500 so proxy backup in nginx works properly when no location is matched.
  
### New features:
- Added health deployment that doesn't restart, allowing for easier debugging.
- Improvements to NFS permissions

### Bug fixes:
- Improvements for health regarding processes when new tenants are created while already running.


## 1.5.0 - 2023-10-24

### Breaking Changes:
- No change.
  
### New features:
- `action_logs` added to pod object along with logs endpoint for detailed audit of actions done on pod_id.

### Bug fixes:
- Fixed some user nested validation errors not showing proper error messages.
- Ensure pods always save logs
- Fixed migrations for action_logs and how it works if logs are empty.
- Better normalized paths.


## 1.4.0 - 2023-07-06

### Breaking Changes:
- No change.
  
### New features:
- Better certs.

### Bug fixes:
- No change.


## 1.3.2 - 2023-06-30

### Breaking Changes:
- No change.
  
### New features:
- Traefik proxying now automatically creates certificates at runtime for each subdomain (meaning each pod).
- Service no longer requires initial, or any, manual certificate creation.
- Some edits for Neo4j as it requires a injected cert.
- Changes for local dev as it's now different from deployment.

### Bug fixes:
- No change.


## 1.3.1 - 2023-06-06

### Breaking Changes:
- Changed image declarations from `custom-myuser/myimage` to `myuser/myimage`.
- `neo4j` and `postgres` templates are now under `template/neo4j` and `template/postgres`.
- Status changes: `RUNNING` -> `AVAILABLE`, `SHUTTING_DOWN` -> `DELETING`, `CREATING_CONTAINER (and volume)` -> `CREATING` 

### New features:
- Added volume and snapshot support/utils/models/etc. Using nfs pvc storage to volume mount block storage to running pods.
    - Users can share volumes and collaborate live on the same storage. Snapshots allow users to take copies of volumes for data versioning purposes.
- Automatic creation of nfs backend with Files along with secure PKI access throughout.
- New model schema to reduce replicated code and have a consolidated method to update models.
- Rewrote nfs health code to reduce number of calls to Files from each tenant + volume to once per health check run.
- Added siteadmintenant to allow for site wide database configs.
- Added database allowlist
- Added `develop_mode` config to easily turn nfs/other features off and on.

### Bug fixes:
- Changed how datetime is setup to validate OpenAPI spec properly.
- Changed namespace code which was causing breaks.
- Added testing for new features and improved testing for old.
- Additional changes for Postgres
- Reworked auth slightly to make it simpler


## 1.3.0 - 2023-03-09

### Breaking Changes:
- Yes.

### New features:
- Since last changelog, now using Traefik as backend, Postgres workaround, Graphdb workarounds as well.

### Bug fixes:
- Yes.


## 0.30.4 - 2022-06-09

### Breaking Changes:
- Yes.

### New features:
- Fixed nginx routing and resolver. 
- Pods are now routed based on pods.url attr. mypod.pods.tacc.develop.tapis.io is the uri format.

### Bug fixes:
- Yes.


## 0.30.3 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Added in proper health and spawner pods so users don't have to manually start the scripts.
- Added check in health.py to wait for database connection instead of failing.

### Bug fixes:
- Yes.


## 0.30.2 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Now includes pre-baked certs with the option to create certs at run time if you do have cert-manager (requires code edits. Ask Christian.)

### Bug fixes:
- Yes.


## 0.30.1 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Now including certs in Neo4j pods so users can make encrypted calls and we can intercept subdomain.
- Changed pods-main to pods-api. No concept of "main" anymore. Just api components.
- Improved serviceaccount. No longer cluster level, only namespace level, easier to manage this way.
- Updated logic for updating configmap. Now checks if old configmap == new. If not update. Tested, no issue in updating configmap hundreds of time per second.
- Init container support is now working.
- Neo4J now has example code for setting volume mounts in kubernetes_templates.

### Bug fixes:
- Yes.


## 0.30.0 - 2022-05-24

### Breaking Changes:
- Yes.

### New features:
- Optimized TapisModel model further. Simplified store access. Simplified running sqlalchemy commands. SqlAlchemy 2.0 compliant.
- Service is now called pods, changed everywhere.
- Added custom pod_template image configuration alongside database.
- Pods can now be dynamically exposed via Nginx through HTTP or TCP.
- Nginx hot reload implemented through health.py and nginx pod livenessProbe.
- Multiple slashes in URL path are now simplified by redirect middleware.
- Added permissions on object along with permission checking in authorization.
- Revealing /docs/redoc/openapi.json now.
- Added new model functions (display, get_permissions, etc.)
- Added set/delete permission functions that ensure there is always one ADMIN per pod.
- Added more configuration for cpu/mem limit and request.
- Better error handling for pods. Some status messages. More in progress.
- pods/{pod_id}/logs endpoint added.
- pods/{pod_id}/permissions endpoint added.
- Added in TapisMiddleware 2.0.
- Now based off PyPi derived tapipy and tapisservice rather than Flaskbase. Allows for Python:3.10
- Support now for more than just Neo4j, also custom images.

### Bug fixes:
- Yes.


## 0.0.3 - 2022-05-03

### Breaking Changes:
- Yes.

### New features:
- Auth!
- Updated flaskbase-plugins to flaskbase-fastapi. This uses a new TapisMiddleware that deals with authn/authz.
- Removed duplicate req_utils stuff. Only have error handler locally now, all other utils from flaskbase (including global g)

### Bug fixes:
- Yes.


## 0.0.2 - 2022-05-02

### Breaking Changes:
- Yes.

### New features:
- Now using SQLModel with SqlAlchemy ORM doing database calls rather than custom made things.
- SQLModel has validation along with helper functions along with attrs for tenant/site schema/db selection.
- Basic CRUD ops for TapisModel.
- Using Alembic for database migrations. Will configure initial databases as well, over all tenants and sites for the base_url.
- Spawner working and creating Neo4j pods. No user facing auth yet. Passwords model created to store that stuff. Needs encryption.
- Health script takes care of checking on all pods in Kubernetes we need to manage. Updates pod statuses, removes stuff.
- kubernetes_utils greatly improved. Much more readable, easy functionality.
- kubernetes_utils added service creation/delete functions.
- Added Neo4J creation function to specify exactly what we need + create service for Neo4J database_type.

### Bug fixes:
- Yes.

## 0.0.1 - 2022-04-08

### Breaking Changes:
- Init. Nothing to break.

### New features:
- Rabbitmq initialization working
- Channels are added and simplified from Abaco implementation
- TapisMiddleware is declared
- FastAPI Global middleware for flask-like g object.
- Error handling with FastAPI. Ok and Error messages routed properly.
- DEV_TOOLS flag in Makefile for volume mount, and auto jupyter lab creation + port reveal.
- Makefile for minikube deployment/build/cleaning.

### Bug fixes:
- Init. Nothing to fix.

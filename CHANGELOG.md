# Change Log
All notable changes to this project will be documented in this file.


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

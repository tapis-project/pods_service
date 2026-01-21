# Pods Service Development Docs
Most of this is quite off the cuff. A lot is still in flux/now invalid. Will need to make another pass down the line to figure out what should be deleted/added/updated.


## Ideas to be aware of.
#### Pod creation
The goal of this service is to create a pod for a user based on the information given to us through the API. The pod creation is done using the Python `Kubernetes` client. With the tools in `kubernetes_utils.py` we're able to create, delete, update, and do whatever we want to Kubernetes pods, services, configmaps, certs, and more. 

The top-level overview of pod creation works is as follows. A user create a pod, by default said pod's `status_requested` is set to `ON`. In the POST to `/pods`, the service will create a database entry for the pod, and send a message with RabbitMQ requesting a new pod. That message will be read by `spawner.py`, in the `spawner` pod. The spawner will take the message and create the pod. After that, `health.py` in the `health` pod will take care of everything else. Health will poll every X seconds. It'll update pod database information based on what's happening to the pod ("Completed", "Running", new logs, etc). Health will also take care of updating the `pods-nginx` configmap with information found during the "healthcheck".


##### Pod Workflow - But more precise now.
**status_requested**: This can be user set to `ON`, `OFF`, or `RESTART`. This is used as an overall, "what do we want to do with the pod" field. Instead of using only the pod `status` field, we can use this to control workflow.
- **ON** - Create pod and get to `RUNNING` status. Default during `create_pod`. Can also be set by `start_pod`.
- **OFF** - Attempts to delete and get to `STOPPED` status. Can be set by `stop_pod`. This will disrupt and pod creation currently happening.
- **RESTART** - Functions as `OFF`. Set `status_requested` to `ON` once pod hits `STOPPED` status.

**status**: This is the internal state of the pod itself, what is actually happening to it, either in regards to only the service, or reflecting back the pod's status from Kubernetes itself.

- **Internal Statuses**
  - **REQUESTED** - Put into status when ON is noticed, either during `create_pod` or healthcheck.
  - **SPAWNER_SETUP** - Set when request msg is read and spawner attempts to create container
  - **CREATING_CONTAINER** - Set after create_pod and create_service is ran
  - **SHUTTING_DOWN** - Set once health starts trying to delete

- **Kubernetes Controlled Statuses**
  - **RUNNING** - Based off of Kubernetes
  - **ERROR** - Based off of Kubernetes
  - **COMPLETE** - Based off of Kubernetes
  - **STOPPED** - If pod + service not in Kubernetes, then status is `STOPPED`

**status_container**: Not that useful, but as we're creating Kubernetes pods, each pod is X containers (usually only 1 in our case). We reflect that for the users to have more data to debug. If a pod has container info, we display it (assumes only 1 container thus far).

---
#### Custom image allowlist - WIP
Pods are started with a user selected image. If that image is not one of our templated images, then the user is using a "custom" image. To ensure that we're not running absolutely whatever and giving it certs + domain access, we need to put images on an allowlist. If the custom image is allowed, then it runs, otherwise we let users know that they should message us to add it to the allowlist.

Allowlist should be created globally, and also per tenant. Globally so that we can grant the entire service access to an image, and tenant-based so tenants can add images as they see fit (These still must be vetted by us though). Global allowlist is the first priority. As of June 22, allowlist is hardcoded globally.

When using a custom image, users are allowed to specify environment variables as they see fit. The pods service will expose port 5000 to the world.

---
#### Templated image pods
As mentioned, users can create custom imaged pods. Users can also create templated pods. These are pods that we have explicit code created for. For example, a user can specify "neo4j" as the pod image. We understand that as a templated pod and create a Neo4J instance that has all settings, environment variables, and certs worked out for. In this case we would need to use TCP rather than HTTP, expose the bolt port rather than port 5000, add in certs for TLS, and configure the Neo4J instance for said certs and also for initial account creation.

These are managed in code currently and require a new deployment for the changes to take place.

**Templated database pod extras:** To note, when we create templated databases for example, we'll create both a user and admin user/pass combo in the database so that we in the future can create a Datasets service to manage the datasets.

---
#### All powerful health.py
The `health` pod that runs health.py has the important job of keeping the API up to date with what's happening in Kubernetes and running healthchecks to clean up Kubernetes or the database when needed.

When running, health.py ensures first that the database's information is synced with the information gathered from Kubernetes. Health updates logs, status, and status_container. It'll also delete any services and pods without database records. Once the database is in sync with information from Kubernetes, health.py goes through the database and ensures on entries there exist in Kubernetes (pods, services, etc.). Health will delete pods with `status_requested` equal to `OFF` at this point. Health also manages the nginx configmap, based on pod information and status, it'll rewrite the config when changes are needed and the Kubernetes will propagate the changes to Nginx.

**Pod service hot-reloading** - An important part to all of this is the fact that pods can live without the API. Health links the API to Kubernetes, but when the API is down, the pods will still exist. Meaning that the service can be restarted, and as long as the pod database information persists, everything will continue on working. Meaning that we can update the API with no damage to the pods except when database schema changes are required. At that point we must run a migration.

---
#### Nginx Hot-Reloading 
In order to serve our pods through Nginx in real time, the service requires a way to edit and redeploy the service's Nginx configmap with no downtime. Nginx+ (plus) does exist and actually gives users an API to manage a specificed nginx instance. Unfortunately we're not using Nginx+. We still need to modify the Nginx config.

Taking advantage of Kubernetes, we can use the `pods-nginx` configmap we create when deploying Nginx in the first place. Our all powerful health.py has essentially full access to Kubernetes, meaning we can edit the `pods-nginx` configmap as we see fit. Said configmap's edits are reflected inside all pods with the configmap according to the kubelet sync period (Kubernetes thing. Basically polls every x seconds). By default that means the configmap will be updated every 60 seconds inside the Nginx pod. **Note:** k8 configs are not reloaded when using configmap subpath directive in pod yaml.

The config is now updated, but we still need to redeploy Nginx so it uses the new config (Nginx uses config it was initialized with). Using a Kubernetes `livenessProbe`, we can run `service nginx reload` every 30 seconds. This restarts Nginx with the new config with no service disturbances.

With these tricks in place, the Nginx instance will update every 1-2 minutes (Waiting for 60 seconds config update + nginx reload). We should look into making this process faster, but it's currently a good solution to the problem that "just works" in a very reproducible way.

**Notes:** There is one error. Nginx when reloading expects every server to be accessible, if we just deleted one or if it's broken, we get an error. Issue logged. 

**Testing:** To note, I tested 500 configmap updates at once and Kubernetes was fine with it, finished in sub 3 seconds, updates updated based on when the update request occured. Doesn't seem like any problems here.

**Nginx configmap creation**
In order to quickly edit the Nginx configmap using data from our database, we use a Jinja2 template to create new config entries for each pod an entry is required for. This all occurs during our healthchecks in health.py. We go through the database, figure out which pods require Nginx entries, and create the config using our template.

**Nginx Subdomain TCP/HTTP Mapping**  
To note with nginx. We create configs for two types of protocols, HTTP and TCP. We have to set the `protocol` field on a pod object to change which to use, but once we dictate what protocol to use, the Jinja2 template will create entries for the correct protocols. So that we can create the config once, for all pods, and deploy it.

---
#### Pod URL creation/schema
We do need to give each pod a URL to use. Methods other services use, along with potential candidates are as follows:

- `(id).databases.neo4j.io` - AuraDB (neo4j) uri pattern (uses TLS)
- `(id).mongodb.net` - Atlas (mongodb) uri pattern (uses TLS)
- `(podid).pods.tapis.io` - Can't do this because dev env needs to route to dev env nginx for example.
- `(podid).environ.tapis.io` - Easy technically, but have to make sure pod ids don't overlap tenants. Jarring in terms of readability compared to usual site/tenant uri pattern.
- `(podid).pods.environ.tapis.io` - Easy to implement, we'd need certs, no tenant can be named "pods".
- `(podid).pods.tenant.environ.tapis.io` - Already implemented, need certs. No collisions.

We went for the final option, `(podid).pods.tenant.environ.tapis.io`, so that our usual `tenant.environ.tapis.io` pattern isn't modified. Added an additional `.pods.` part on top of that so that we have future flexibility in subdomain handling in case we want to introduce any new services, other than pods.

---
#### Certs
As we're hosting content that will be accessed over the internet, we'll need to discuss certificates for secure connections. A crux of our Nginx routing requires that all TCP connections use TLS so that we can read the SNI information from the connection and map a particular subdomain to a particular kubernetes pod. That unfortunately means that we'll need certificates for each pod using TCP due to TLS. Basically, we need certs.

Currently we have tapis wide certificates for `*.environ.tapis.io`. These are only valid for this URL level. Using Multi-Domain Wildcard SSL Certificates, we can have one certificate manage multiple domains, for example one cert could manage `*.tacc.environ.tapis.io`, `*.dev.environ.tapis.io`, and `*.environ.tapis.io` (we don't currently use multi-domain certs though).

The question still lies though, how are we going to provide certificates so that our user's pods can do everything with TLS? Correct me if I'm wrong, but I believe if our provider deems `mypod.pods.tacc.develop.tapis.io` malicious, they could cancel our cert all together. This means we definitely do not want one all encompassing cert, not to mention each pod would require the certs `.crt` and secret `.key` files.

This would seem to mean that we need a new cert per pod. What is the feasibility of this however? We can manage certificates with cert-manager. We need a cluster so we can actually deploy that though. We can create certs in the service, and save them to a Kubernetes secret. When the secret is updated, so is the secret in the pod.

A question. Do we want a cert for the entire environment, the tenant, or per pod?

Certs in each case would encompass:

- **Environment:**
  - `*.pods.tacc.develop.tapis.io`
  - `*.pods.icicle.develop.tapis.io`
  - `*.pods.dev.develop.tapis.io`
  - etc.
- **Tenant:**
  - `*.pods.tacc.develop.tapis.io`
- **Pod:**
  - `mypodname.pods.tacc.develop.tapis.io`

**Questions:**  
- At least temporarily, how do we create a cert? Ask Don again?

**Later on questions:**  
- Are there audits here?
- Who do we let know that we're doing this stuff? 
- Do we need to run all of this by anyone? Especially if we're using letsencrypt and creating brand new certs as we want.
- Cert one for *.pods.tacc.develop.tapis.io wouldn't affect cert two for *.develop.tapis.io. Correct?
- Who's our CA? Are we? Is that what Don's doing to create certs? Or is that going through Route53 or something like that?

---
#### Pod isolation
- **Cert isolation** - WIP
	- We can't have a bad cert affect our `tapis.io`. Cert errors could be bad.
	- We should consider creating a new cert per pod.
	- Self-signed certs are possible, but they cause browser security prompts and requires workarounds when using database drivers.
	- We can't have unsecured connections. Again browser issues. But for TCP connections, we require TLS so that we can read the connection's SNI and grab the connections subdomain and map using that.

- **Service + Network isolation** - WIP
  - Pods shouldn't be able to use any services at all. No service Egress. Only service Ingress from pods-nginx.
  - Pods shouldn't be able to make any calls via ip. They should have internet access though.
  - These can be setup with K8 NetworkPolcies. Only specific Kubernete CNI's take NetworkPolicies into account though.
      - Currently our Container Network Interface (CNI), “flannel” doesn’t give us access to use Kubernetes features such as “NetworkPolicy”.
      - https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/

  - Can also isolate with namespace alone, pods could still access other things in the namespace though. We could have a namespace per tenant, but that of course gets very tricky.
- **Pod isolation**
	- Pods shouldn't have k8 control or access to other pods.
	- Pods require specific K8 roles in order to use the Kubernetes client, we ensure they do not have those roles.
- **Environment Variable isolation**
    - Block access to default environment variables.
	- Kubernetes throws some extra environment variables into pods, we overwrite those. 

---
#### Postgres + Alembic + SQLModel + Fastapi


#### Endpoints.
This is white boarding when discussing future endpoints.
View [live-docs](https://tapis-project.github.io/live-docs/?service=Pods) for current endpoints.  



---
## Secrets Syntax Reference

### Pod `secret_map` Syntax

| Syntax | Description | Example |
|--------|-------------|---------|
| `${secret:name}` | User's own secret from SK | `${secret:my_db_pass}` |
| `${secret:user:name}` | Another user's secret from SK (requires permission) | `${secret:jsmith:shared_key}` |
| `${pods:random:int_length}` | Generate random password (8-128 chars), persisted to DB | `${pods:random:32}` |
| `${pods:url}` | Pod's full URL | `mypod.pods.tacc.tapis.io` |
| `${pods:tapis_url}` | Base Tapis URL | `tacc.tapis.io` |
| `${pods:networking:net_name:FIELD}` | Networking field (url, hostname, port, protocol, tapis_url) | `${pods:networking:default:port}` |
| `literal_value` | Plain text | `my-config-value` |

### Template `secret_map` Syntax (Only allows placeholder secret values)

| Syntax | Description | Example |
|--------|-------------|---------|
| `${:?description}` | Required - pod must override | `${:?Database password}` |
| `${pods:default:value:?description}` | Optional - uses default if not overridden | `${pods:default:localhost:?DB hostname}` |
| `literal_value` | Plain text for non-secret config | `production` |

**Note:** Templates cannot use `${secret:name}` - they define placeholders that pod creators override.

### Environment Variables & Config File References

> **⚠️ Important:** `${pods:url}`, `${pods:tapis_url}`, `${pods:random:int_length}`, `${pods:networking:...}` only work in `secret_map`. To use these values elsewhere, define them in `secret_map` first:
> ```python
> "secret_map": { "MY_URL": "${pods:url}" },
> "environment_variables": { "APP_URL": "${pods:secrets:MY_URL}" }
> ```
> **Only `${pods:secrets:KEY}` works in `environment_variables` and config files**

##### Self-Documenting References with `:?description`

You can add descriptions to `${pods:secrets:KEY}` references in `environment_variables` and `config_content`. Descriptions are **informational only** and stripped during interpolation:

| Syntax | Description | Example |
|--------|-------------|---------|
| `${pods:secrets:KEY}` | Basic secret reference | `${pods:secrets:DB_PASS}` |
| `${pods:secrets:KEY:?description}` | Secret reference with description | `${pods:secrets:DB_PASS:?Database password}` |

**Example: Self-documenting config file**
```ini
[database]
host = ${pods:secrets:DB_HOST:?Hostname of the database server}
port = ${pods:secrets:DB_PORT:?Database port (default 5432)}
password = ${pods:secrets:DB_PASS:?Database password - obtain from admin}
```

**Example: Self-documenting environment variables**
```python
"environment_variables": {
    "DATABASE_URL": "postgres://${pods:secrets:DB_USER:?DB username}:${pods:secrets:DB_PASS:?DB password}@${pods:secrets:DB_HOST:?Hostname}/mydb",
    "API_KEY": "${pods:secrets:API_KEY:?Get from https://api.example.com/keys}"
}
```

The descriptions help collaborators understand what each secret is for without needing external documentation. After resolution, only the values remain.


### Pod Networking Fields
Reference pod networking dynamically with `${pods:networking:<networking_name>:FIELD}`, where fields are below.

| Field | Description | Example Value |
|-------|-------------|---------------|
| `url` | Full URL | `mypod.pods.tacc.tapis.io` |
| `hostname` | Same as url | `mypod.pods.tacc.tapis.io` |
| `tapis_url` | Base Tapis URL | `tacc.tapis.io` |
| `port` | Port number | `5000` |
| `protocol` | Network protocol | `http` |


**Shorthands:**
- `${pods:url}` = `${pods:networking:default:url}`
- `${pods:tapis_url}` = `${pods:networking:default:tapis_url}`

### Random Password Generation

`${pods:random:int_length}` generates a secure random password (8-128 chars) on first resolution, then persists to DB. Characters: `a-zA-Z0-9!@#$%^&*`

### Example: Template → Pod Flow
```python
# Template secret_map (defines structure)
{
    "DB_PASSWORD": "${:?Database password}",
    "DB_HOST": "${pods:default:localhost:?Database host}"
}

# Pod secret_map (provides values)
{
    "DB_PASSWORD": "${secret:my_db_secret}",
    "DB_HOST": "prod-db.example.com"
}

# Pod environment_variables (consumes)
{
    "DATABASE_URL": "postgres://user:${pods:secrets:DB_PASSWORD}@${pods:secrets:DB_HOST}/db"
}
```

### Example: Networking & Random Password
```python
# Pod with OAuth callback using pod URL and generated secret
{
    "pod_id": "myapp",
    "image": "myorg/webapp:1.0",
    "secret_map": {
        "SESSION_SECRET": "${pods:random:64}",
        "OAUTH_CALLBACK": "https://${pods:url}/auth/callback",
        "TAPIS_BASE": "${pods:tapis_url}"
    },
    "environment_variables": {
        "SESSION_SECRET": "${pods:secrets:SESSION_SECRET}",
        "OAUTH_REDIRECT_URI": "${pods:secrets:OAUTH_CALLBACK}",
        "TAPIS_URL": "${pods:secrets:TAPIS_BASE}"
    },
    "volume_mounts": {
        "/etc/app/config.yaml": {
            "type": "ephemeral",
            "config_content": "oauth:\n callback_url: ${pods:secrets:OAUTH_CALLBACK}\n session_secret: ${pods:secrets:SESSION_SECRET}"
        }
    }
}

# After resolution (pod URL = myapp.pods.tacc.tapis.io):
# SESSION_SECRET env var -> "aB3xK9!@mNpQ..." (generated, persisted)
# OAUTH_REDIRECT_URI env var -> "https://myapp.pods.tacc.tapis.io/auth/callback"
# TAPIS_URL -> "tacc.tapis.io"
```

### Secrets Model Fields
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `readable` | bool | `True` | If `False`, value cannot be retrieved via API (pod injection still works) |
| `writable` | bool | `True` | If `False`, value cannot be updated (write-once) |

### Secret Resolution Flow

When a pod is started, secrets are resolved **at the API layer** before being sent to the spawner. This design allows remote spawners to function without direct access to the Security Kernel.

Resolution order:
1. **Random passwords** (`${pods:random:int_length}`) - generates and persists to DB
2. **Pod networking** (`${pods:networking:*}`, `${pods:url}`) - resolves pod URLs/ports
3. **SK secrets** (`${secret:name}`) - fetches from Security Kernel

Flow - 
1. Pod requested through start, or create, or central health monitoring edge changes.
2. Derive final pod + template with SK. Resolve secrets (resolve_secret_map) and put spawn msg in queue. 
3. Spawner reads msg and creates pod, using inject_secrets_into_env_vars and interpolate_config_content.

#### Viewing Resolved Secrets

Use the `/derived` endpoint with `resolve_secrets=true` to preview how secrets will be interpolated (admin only):

```bash
GET /pods/{pod_id}/derived?resolve_secrets=true&include_configs=true
# response with env and config using proper final secrets
```

#### Detecting Unresolved Patterns

Use `check_unresolved=true` on GET endpoints to detect any `${...}` patterns that weren't resolved:

```bash
GET /pods/{pod_id}?check_unresolved=true
# Returns metadata.unresolved_patterns if any patterns remain
```

The `/derived` endpoint automatically includes unresolved pattern detection in its response metadata.

---

## Placeholder Parsing & Validation Reference

The following table documents the functions that parse and validate placeholder/secret patterns, and which endpoints use them:

| Function | Purpose | Patterns Handled | Endpoints That Use It |
|----------|---------|------------------|----------------------|
| `parse_secret_reference()` | Parse a single secret_map value into structured reference | `${secret:name}`, `${secret:user:name}`, `${pods:default:val:?desc}`, `${:?description}`, literals | All validation and resolution functions |
| `get_placeholder_warnings()` | Detect unresolved placeholders in secret_map | `${pods:default:...}`, `${:?...}`, inline placeholders | `POST /pods` (create pod) |
| `validate_template_secret_map()` | Validate templates don't contain direct secret refs | `${secret:...}` (invalid in templates) | `POST /templates/{id}/tags`, template tag validation |
| `validate_environment_placeholders()` | Validate `${pods:secrets:KEY}` refs exist in secret_map | `${pods:secrets:KEY}` with missing keys | `POST /pods`, template validation |
| `get_config_secret_map_warnings()` | Warn when secret_map-only syntax used in wrong place | `${pods:default/networking/url/random}` in env vars | `POST /pods` (create pod) |
| `validate_secret_map()` | Validate secret format and ownership | All `${secret:...}` patterns | Pod creation validation |
| `resolve_secret_map()` | Resolve all secret_map values to final strings | All patterns → resolved values | `POST /pods` (on start), `GET /pods/{id}/derived?resolve_secrets=true` |
| `resolve_random_passwords()` | Generate and persist random passwords | `${pods:random:N}` | `POST /pods` (before db_create) |
| `resolve_pod_networking()` | Resolve pod URL/networking references | `${pods:networking:...}`, `${pods:url}`, `${pods:tapis_url}` | `POST /pods` (before db_create) |
| `inject_secrets_into_env_vars()` | Inject resolved secrets into environment_variables | `${pods:secrets:KEY}` or `${pods:secrets:KEY:?desc}` → actual value | `GET /pods/{id}/derived`, spawner |
| `interpolate_config_content()` | Inject secrets into config file content | `${pods:secrets:KEY}` or `${pods:secrets:KEY:?desc}` in config_content | `GET /pods/{id}/derived`, spawner |
| `validate_pod_secret_map_against_template()` | Validate pod overrides all required template placeholders | Required `${:?...}` must be overridden | `POST /pods`, `GET /pods/{id}/derived` |
| `detect_unresolved_patterns()` | Detect ANY remaining `${...}` patterns | All `${...}` syntax | Internal use by `check_pod_unresolved_patterns()` |
| `check_pod_unresolved_patterns()` | Helper to check pod config for unresolved patterns | All `${...}` syntax | `GET /pods/{id}`, `GET /pods/{id}/derived`, `POST /pods` |

### Pattern Resolution Flow

```
Pod Creation (POST /pods):
  1. expand_short_secret_references() - ${secret:name} → ${secret:user:name}
  2. resolve_random_passwords() - ${pods:random:N} → generated value
  3. resolve_pod_networking() - ${pods:url}, ${pods:networking:...} → URLs
  4. validate_environment_placeholders() - check ${pods:secrets:KEY} refs exist
  5. detect_unresolved_patterns() - report any remaining ${...} in metadata
  
Pod Start (when status_requested=ON):
  6. resolve_secret_map() - ${secret:user:name} → fetched value from SK
  7. inject_secrets_into_env_vars() - ${pods:secrets:KEY} → resolved value
  8. interpolate_config_content() - ${pods:secrets:KEY} in configs → resolved
```

---

## Template Permissions Reference

Templates support flexible permission sharing through the `permissions` field. Permissions control who can view and use templates.

### Permission Formats

| Format | Scope | Example | Description |
|--------|-------|---------|-------------|
| `username:LEVEL` | User | `jsmith:READ` | Standard user permission |
| `**:READ` | Site-wide | `**:READ` | All users across all tenants (public to entire site) |
| `tenant.<tenant_id>:READ` | Tenant-wide | `tenant.dev:READ` | All users in specified tenant |

### Public Template Restrictions

**Site-wide (`**`) and tenant-wide (`tenant.*`) permissions:**
- Only `READ` level is allowed (for security)
- Only **admins** can set these permissions
- Non-admin users will receive an error when attempting to set public permissions

```python
# Admin sets site-wide public access
POST /pods/templates/{template_id}/permissions
{
    "user": "**",
    "level": "READ"
}

# Admin sets tenant-wide public access for 'dev' tenant
POST /pods/templates/{template_id}/permissions
{
    "user": "tenant.dev",
    "level": "READ"
}
```

### Template Visibility Rules

|  | `**:READ` permission | `tenant.dev:READ` permission | Private Template |
|---------------|-------------------|---------------------------|------------------|
| User in `dev` tenant | ✅ Visible | ✅ Visible | ❌ Not visible |
| User in `tacc` tenant | ✅ Visible | ❌ Not visible | ❌ Not visible |
| User in any tenant | ✅ Visible | Only if in matching tenant | Only if explicitly granted |


### Example: Creating a Public Template

```python
# 1. Admin creates template
POST /pods/templates
{
    "template_id": "postgrestemplate",
    "description": "PostgreSQL database template for all users"
}

# 2. Admin adds template tag with pod definition
POST /pods/templates/postgrestemplate/tags
{
    "tag": "v1",
    "pod_definition": {
        "image": "postgres:15",
        "secret_map": {
            "POSTGRES_PASSWORD": "${:?Database password - required}"
        }
    }
}

# 3. Admin makes template public site-wide
POST /pods/templates/postgrestemplate/permissions
{
    "user": "**",
    "level": "READ"
}

# 4. Any user can now create pods from this template
POST /pods
{
    "pod_id": "my-postgres",
    "template": "postgrestemplate:v1",
    "secret_map": {
        "POSTGRES_PASSWORD": "my-secure-password"
    }
}
```

---

## Volume Mounts Reference

The `volume_mounts` field is an **object keyed by mount path**, enabling template inheritance and partial overrides.

### Mount Types
| Type | Description | Default `read_only` | `source_id` |
|------|-------------|---------------------|-------------|
| `tapisvolume` | Persistent Tapis volume | `false` | Required |
| `tapissnapshot` | Read-only snapshot | `true` | Required |
| `ephemeral` | Inline config (K8s ConfigMap) | `true` | No |
| `pvc` | Raw K8s PVC (admin only) | `false` | Required |

### All Fields
| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | string | Yes | - | `tapisvolume`, `tapissnapshot`, `ephemeral`, `pvc` |
| `source_id` | string | For storage types | - | Volume/snapshot/PVC ID |
| `sub_path` | string | No | `""` | Subdirectory within source to mount |
| `read_only` | bool | No | Varies | Override default read-only behavior |
| `config_content` | string | For ephemeral | - | File content (max 1MB) |
| `config_filename` | string | No | basename of path | Filename in volume (tapisvolume only (so far)) |
| `config_permissions` | string | No | `"0644"` | Unix permissions (octal) |
| `config_update_mode` | string | No | `"always"` | `"always"` or `"once"` |

### Ephemeral vs Tapisvolume with Config
| Feature | ephemeral | tapisvolume + config |
|---------|-----------|---------------------|
| Storage | K8s ConfigMap | NFS Volume |
| Read/Write | **Read-only** | **Read-write** |
| Persists on delete | No | Yes (in volume) |
| Max size | 1MB | Volume limit |
| `config_update_mode` default | `"always"` | `"once"` recommended |

---

### Examples

#### Basic Mounts
```python
"volume_mounts": {
    "/data": {"type": "tapisvolume", "source_id": "my-volume"},
    "/reference": {"type": "tapissnapshot", "source_id": "shared-snap"},
    "/models": {"type": "tapisvolume", "source_id": "ml-data", "sub_path": "v2"}
}
```

#### Ephemeral Config with Secret Interpolation
```python
"secret_map": {"DB_PASS": "${secret:my_db_secret}"},
"volume_mounts": {
    "/etc/app/db.ini": {
        "type": "ephemeral",
        "config_content": "[db]\nhost=localhost\npassword=${pods:secrets:DB_PASS}",
        "config_permissions": "0600"
    }
}
```

#### Tapisvolume with Persistent Config
```python
"volume_mounts": {
    "/app/data": {
        "type": "tapisvolume",
        "source_id": "my-volume",
        "config_content": "[settings]\ninitialized=true",
        "config_filename": "app.conf",        # Written to /app/data/app.conf
        "config_permissions": "0600",
        "config_update_mode": "once"          # Only create if doesn't exist
    }
}
```

#### Multiple Config Files
```python
"volume_mounts": {
    "/etc/app/database.yml": {"type": "ephemeral", "config_content": "host: db\nport: 5432", "config_permissions": "0600"},
    "/etc/app/api.yml": {"type": "ephemeral", "config_content": "endpoint: https://api.example.com"},
    "/etc/app/logging.yml": {"type": "ephemeral", "config_content": "level: INFO"}
}
```

---

### Template Inheritance

**Merge rules:**
1. Pod's mounts merged with template's by path
2. Pod definition **completely replaces** template mount at same path
3. `null` removes inherited mount

```python
# Template definition
POST /pods/templates/webapp/tags
{
    "tag": "v1",
    "pod_definition": {
        "image": "myorg/webapp:1.0",
        "volume_mounts": {
            "/data": {"type": "tapisvolume", "source_id": "shared-data", "config_content": "init=true", "config_filename": "app.conf", "config_update_mode": "once"},
            "/etc/app/config.yml": {"type": "ephemeral", "config_content": "env: prod"}
        }
    }
}

# Pod usage patterns:
# Inherit all fields
{"template": "webapp:v1"}

# Add new mount (template mounts inherited)
{"template": "webapp:v1", "volume_mounts": {"/extra": {"type": "tapisvolume", "source_id": "extra-vol"}}}

# Remove inherited mount
{"template": "webapp:v1", "volume_mounts": {"/etc/app/config.yml": null}}
```

#### Overriding source_id While Keeping Config

**Option 1: Use `template_overrides` (Recommended)**

The `template_overrides` field allows partial overrides without repeating full config:

```python
# Template has volume with config:
# "/data": {"type": "tapisvolume", "source_id": "shared-data", "config_content": "init=true", "config_filename": "app.conf"}

# Override only source_id, keep all other config
{
    "template": "webapp:v1",
    "template_overrides": {
        "volume_mounts": {"/data": {"source_id": "my-volume"}}
    }
}
# Result: /data uses my-volume with original config_content, config_filename, etc.
```

**template_overrides supports:**
- `volume_mounts`: Dict[mount_path, partial_config] - merges fields into existing mount
- `secret_map`: Dict[key, value] - replaces placeholder with your secret reference

```python
# Override both volumes and secrets
{
    "template": "postgres:v2",
    "template_overrides": {
        "volume_mounts": {
            "/var/lib/postgresql/data": {"source_id": "my-pgdata"}
        },
        "secret_map": {
            "POSTGRES_PASSWORD": "${secret:my-pg-password}"
        }
    }
}
```

**Option 2: Full replacement via `volume_mounts`**

When using `volume_mounts` directly, you must **repeat the full config**:

```python
# WRONG: Just changing source_id loses the config
{"template": "webapp:v1", "volume_mounts": {"/data": {"type": "tapisvolume", "source_id": "my-volume"}}}
# Result: /data has no config_content!

# CORRECT: Repeat full config with new source_id
{"template": "webapp:v1", "volume_mounts": {
    "/data": {
        "type": "tapisvolume",
        "source_id": "my-volume",           # Your volume
        "config_content": "init=true",       # Repeat from template
        "config_filename": "app.conf",       # Repeat from template
        "config_update_mode": "once"         # Repeat from template
    }
}}
```

**Tip**: Use `GET /pods/templates/{template_id}/tags/{tag}?include_configs=true` to see the full mount config to copy.

---

### Permission Validation

- **Direct creation**: Missing volume permission → Error
- **Template-defined**: Missing permission → Warning (non-blocking)

---

### Legacy Migration

List format auto-converts to object-keyed. Migration via alembic, user shouldn't need to touch.
```python
# Legacy                                           # Current
[{"type": "tapisvolume", "source_id": "vol1",  →  {"/data": {"type": "tapisvolume", "source_id": "vol1"}}
  "mount_path": "/data"}]
```

---

## Remote Deployment Considerations

When deploying Pods Service with edge/remote spawners, special consideration is needed for secret handling since edge components should not have direct Security Kernel access.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CENTRAL SITE                                       │
│  ┌─────────┐  ┌─────────┐  ┌─────────────┐  ┌──────────────────────────┐    │
│  │   API   │  │   SK    │  │  Database   │  │  Central Health/Coord    │    │
│  └────┬────┘  └────┬────┘  └──────┬──────┘  └────────────┬─────────────┘    │
│       │            │              │                      │                   │
└───────┼────────────┼──────────────┼──────────────────────┼───────────────────┘
        │            │              │                      │
        │   resolve_secret_map()    │                      │
        │◄───────────┘              │                      │
        │                           │                      │
        ▼                           ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MESSAGE QUEUE (RabbitMQ)                             │
│                    (resolved_secrets in message payload)                     │
└─────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           REMOTE/EDGE SITE                                   │
│  ┌───────────────────┐    ┌─────────────────────────────────────────────┐   │
│  │     Spawner       │    │              Health (monitor only)          │   │
│  │  - receives msg   │    │  - updates pod status from K8s              │   │
│  │  - has resolved   │    │  - syncs logs                               │   │
│  │    secrets        │    │  - does NOT resolve secrets                 │   │
│  │  - creates pod    │    │  - does NOT initiate restarts (see below)   │   │
│  └───────────────────┘    └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Edge Management Considerations

Currently and in implementation secrets will be resolved by API as SK access is central only. We need to architect this.
Start and restart from API will be central. But restarts will be completely on the edge, they must be able to run a pod as well.

1. Edge can call a endpoint to request a packet of data for running pod or request central health does something.
2. Edge can use a queue to request central pod start as well.
3. Central health can monitor the edge and when in state to restart it does things rather than edge health. **

I like 3. Edge stays simple and doesn't need more access.
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

"""
Deploy recipes — playbooks for multi-step "finagling" that a single template
tag can't express.

`deploy_from_template` covers anything whose whole shape is a pod/stack
*definition*. But some apps need steps a template body has no room for:
post-boot `exec` (create an admin user, mint a token), a credential injected via
`secret_map`, or wiring a Kubernetes imagePullSecret afterward. Those are captured
here as ordered, copy-pasteable steps so any operator (human or LLM) can reproduce
them well instead of rediscovering the gotchas each time.

Schema (per recipe):
    id, title, when_to_use, why_not_a_template, prereqs[], steps[], gotchas[],
    deliverables[]
Each step: {n, action, api, detail, example}
    action ∈ {create_secret, allowlist_image, create_volume, create_pod, wait,
              exec, verify, wire}
    api    the concrete Tapis Pods call (or "kubectl"/"docker" for client-side)

NOTE ON SCOPE: `exec` steps use POST /pods/{id}/exec, now exposed as the
`exec_pod_commands` MCP tool (every call audited; the caller's token gates it).
Confirm before running writes — the recipe documents *what* to run and *why*; an
MCP client should not fire the exec steps blindly.
"""

# The finagling pattern this class of recipe encodes, stated once:
PATTERN = (
    "env-driven install (no web installer) + credentials injected via secret_map "
    "→ env, then post-boot exec for the one or two steps the image can't do from "
    "env (create admin user, mint token). Prefer post-boot exec over an init/command "
    "script: the image's own entrypoint is finicky and running setup before the app "
    "is ready is racy."
)

GITEA_REGISTRY = {
    "id": "gitea-registry",
    "title": "Deploy a Gitea pod as a private git + OCI/Docker registry",
    "when_to_use": (
        "You want a private container registry (and git host) inside Pods to push "
        "images to and pull from — e.g. to feed the TAPIS_PODS_IMAGEPULLSECRET "
        "mechanism so other pods can run your own images."
    ),
    "why_not_a_template": (
        "A template can define the pod, but Gitea's admin user and registry access "
        "token can only be created by running `gitea admin ...` INSIDE the container "
        "after boot. So this is a template-style pod definition PLUS two post-boot "
        "exec steps. " + PATTERN
    ),
    "prereqs": [
        "You are ADMIN in the tenant (to allowlist an image).",
        "Pick a pod_id; its public URL will be {pod_id}.pods.{tenant-host} with an "
        "auto-provisioned TLS cert.",
    ],
    "steps": [
        {
            "n": 1,
            "action": "create_secret",
            "api": "POST /pods/secrets",
            "detail": "Store the Gitea admin password in Tapis SK — never inline it. "
                      "Injected into the pod via secret_map; readable=true lets you "
                      "retrieve it later for the web UI login.",
            "example": {
                "secret_id": "gitea-admin-pw", "scope": "user", "readable": True,
                "writable": True, "secret_value": "<generate a strong 24+ char value>",
                "description": "Gitea admin password",
            },
        },
        {
            "n": 2,
            "action": "allowlist_image",
            "api": "POST /pods/images",
            "detail": "Images must be on the allowlist (stored WITHOUT a tag).",
            "example": {"image": "gitea/gitea", "tenants": ["*"],
                        "description": "Gitea - git + OCI/docker registry"},
        },
        {
            "n": 3,
            "action": "create_volume",
            "api": "POST /pods/volumes",
            "detail": "Persistent /data — holds repos, the sqlite DB, and registry blobs.",
            "example": {"volume_id": "giteavol", "size_limit": 5120,
                        "description": "Gitea /data"},
        },
        {
            "n": 4,
            "action": "create_pod",
            "api": "POST /pods",
            "detail": "secret_map pulls the SK secret; environment_variables references "
                      "it via ${pods:secrets:KEY} AND drives Gitea's env-based install "
                      "(GITEA__section__KEY). INSTALL_LOCK=true skips the web installer. "
                      "ROOT_URL MUST be the pod's public https URL or the registry's "
                      "Bearer-token realm breaks. networking.tapis_auth MUST be false: "
                      "`docker login` uses the registry's own Basic/Bearer auth, not "
                      "Tapis OAuth — Gitea's own login guards it.",
            "example": {
                "pod_id": "gitea",
                "image": "gitea/gitea:1.22",
                "secret_map": {"ADMIN_PW": "${secret:gitea-admin-pw}"},
                "environment_variables": {
                    "USER_UID": "1000", "USER_GID": "1000",
                    "GITEA__server__PROTOCOL": "http",
                    "GITEA__server__HTTP_PORT": "3000",
                    "GITEA__server__ROOT_URL": "https://gitea.pods.<tenant-host>/",
                    "GITEA__server__DOMAIN": "gitea.pods.<tenant-host>",
                    "GITEA__security__INSTALL_LOCK": "true",
                    "GITEA__database__DB_TYPE": "sqlite3",
                    "GITEA__service__DISABLE_REGISTRATION": "true",
                    "GITEA__service__REQUIRE_SIGNIN_VIEW": "true",
                    "GITEA__packages__ENABLED": "true",
                    "GITEA_ADMIN_PASSWORD": "${pods:secrets:ADMIN_PW}",
                },
                "volume_mounts": {"/data": {"type": "tapisvolume", "source_id": "giteavol"}},
                "networking": {"default": {"protocol": "http", "port": 3000,
                                           "tapis_auth": False}},
            },
        },
        {
            "n": 5,
            "action": "wait",
            "api": "GET /pods/{pod_id}",
            "detail": "Poll until status == AVAILABLE and networking.default.cert_ready "
                      "== true before exec/curl.",
        },
        {
            "n": 6,
            "action": "exec",
            "api": "POST /pods/{pod_id}/exec  (exec_pod_commands MCP tool; audited)",
            "detail": "Create the admin user, reading the injected password from env. "
                      "exec runs as root; drop to the git user with su-exec. Wrap in "
                      "sh -c so $GITEA_ADMIN_PASSWORD expands.",
            "example": {"commands": [[
                "sh", "-c",
                'su-exec git gitea admin user create --admin --username cgarcia '
                '--email you@example.com --password "$GITEA_ADMIN_PASSWORD" '
                '--must-change-password=false',
            ]]},
        },
        {
            "n": 7,
            "action": "exec",
            "api": "POST /pods/{pod_id}/exec  (exec_pod_commands MCP tool; audited)",
            "detail": "Mint a registry token (scope write:package includes read). "
                      "--raw prints just the token on stdout; capture it and immediately "
                      "store it in a secret rather than logging it.",
            "example": {"commands": [[
                "sh", "-c",
                'su-exec git gitea admin user generate-access-token --username cgarcia '
                '--token-name registry --scopes "write:package" --raw',
            ]], "then": "POST /pods/secrets {secret_id: gitea-registry-token, "
                        "secret_value: <captured>, readable: true}"},
        },
        {
            "n": 8,
            "action": "verify",
            "api": "curl https://{host}/v2/",
            "detail": "Registry is live when /v2/ returns 401 with header "
                      "'www-authenticate: Bearer realm=\"https://{host}/v2/token\"'. "
                      "Full check: Basic auth (user:token) to that realm returns a JWT; "
                      "using it as Bearer on /v2/ returns 200 → `docker login` will work.",
        },
        {
            "n": 9,
            "action": "wire",
            "api": "kubectl (pods namespace) + pod env",
            "detail": "Let other pods PULL your images via the built-in imagePullSecret "
                      "mechanism. The k8s secret name is fixed: "
                      "tapis-pods-imagepullsecret-<username> (username @→at, .→dot). "
                      "Then set env TAPIS_PODS_IMAGEPULLSECRET=<username> on the pulling "
                      "pod; that user must have APPROVEDADMIN on it.",
            "example": {
                "kubectl": (
                    "kubectl -n <pods-namespace> create secret docker-registry "
                    "tapis-pods-imagepullsecret-cgarcia "
                    "--docker-server=gitea.pods.<tenant-host> "
                    "--docker-username=cgarcia --docker-password=<token> "
                    "--docker-email=you@example.com"
                ),
                "docker": (
                    "docker login gitea.pods.<tenant-host> -u cgarcia -p <token>; "
                    "docker tag img gitea.pods.<tenant-host>/cgarcia/img:latest; "
                    "docker push gitea.pods.<tenant-host>/cgarcia/img:latest"
                ),
                "pod_env": {"TAPIS_PODS_IMAGEPULLSECRET": "cgarcia"},
            },
        },
    ],
    "gotchas": [
        "networking.tapis_auth MUST be false — otherwise the Tapis OAuth proxy "
        "intercepts and docker login/pull can't authenticate.",
        "ROOT_URL must equal the pod's public https URL or the registry Bearer realm "
        "points at the wrong place and pushes fail.",
        "exec runs as root; run the gitea CLI as the git user via su-exec.",
        "Image allowlist entries are stored WITHOUT the tag (gitea/gitea, not :1.22).",
        "The imagePullSecret k8s secret name is not free-form: "
        "tapis-pods-imagepullsecret-<username>, and the named user needs APPROVEDADMIN "
        "(admin-approved level) on the pulling pod.",
        "exec (steps 6-7) runs via the exec_pod_commands MCP tool (audited); confirm "
        "before firing it rather than auto-running the init.",
    ],
    "deliverables": [
        "Registry host: https://{pod_id}.pods.<tenant-host>",
        "SK secret gitea-admin-pw (web UI login) and gitea-registry-token (docker/pull).",
        "docker login + push commands and the imagePullSecret wiring for pulls.",
    ],
}

RECIPES = {r["id"]: r for r in (GITEA_REGISTRY,)}


def list_recipes() -> list:
    """Compact index: id, title, when_to_use — cheap to scan before fetching one."""
    return [
        {"id": r["id"], "title": r["title"], "when_to_use": r["when_to_use"],
         "steps": len(r["steps"])}
        for r in RECIPES.values()
    ]


def get_recipe(recipe_id: str) -> dict | None:
    return RECIPES.get(recipe_id)

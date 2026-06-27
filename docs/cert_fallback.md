# Wildcard fallback cert — make direct hits "pretty" during ACME provisioning

## Problem

Each pod domain (`<pod_id>.pods.<tenant>.<base>`, e.g. `mypod.pods.icicle.tapis.io`) gets
its **own** Let's Encrypt cert, issued on the fly by Traefik (`certResolver: tlsletsencrypt`)
the first time the domain is hit. During that ~20s issuance window Traefik has no
SNI-matching cert for the host, so it serves the **default certificate** instead.

Today the default certificate is a self-signed *snakeoil* cert
([deploymentTemplate/traefik.yml](../deploymentTemplate/traefik.yml) →
`tls.stores.default.defaultCertificate` → `/tmp/ssl/tls.crt`, from the `pods-certs` secret).
Because its SAN doesn't match the pod hostname, a browser navigating straight to
`https://mypod.pods.<tenant>...` gets a **"your connection is not private"** interstitial —
*before* any HTTP routing, so our `/pod-splash` page never renders for direct hits.

(The in-app PodLaunch holding page sidesteps this for links by waiting on the valid-cert app
domain; this doc is about making **direct** browser hits pretty too.)

## Fix: serve a valid **wildcard** as the default certificate

If the default certificate is a *real* wildcard whose SAN covers the pod hostname, then during
the per-pod ACME window the browser gets valid TLS from the wildcard and Traefik serves our
`/pod-splash` page — **no warning**. Once the per-pod LE cert is issued, Traefik prefers the
SNI-matching specific cert over the default, so per-pod certs are unaffected.

The wiring already exists — you only need to put a wildcard into the default-cert slot:

```yaml
# deploymentTemplate/traefik.yml — tls.stores.default.defaultCertificate
tls:
  stores:
    default:
      defaultCertificate:
        certFile: /tmp/ssl/tls.crt   # <- make the pods-certs secret hold the WILDCARD, not snakeoil
        keyFile:  /tmp/ssl/tls.key
```

So in practice: **replace the contents of the `pods-certs` k8s secret with the wildcard
cert/key** (see [deploymentTemplate/secrets.yml](../deploymentTemplate/secrets.yml)). No Traefik
config change is required beyond that.

## TLS termination point (resolved): Traefik

nginx fronts Traefik but only does **SNI-sniffing stream passthrough** (L4) — it does **not**
terminate TLS for pod domains. **Traefik terminates.** So the wildcard/default cert belongs on
**Traefik** (this doc), not nginx.

## ⚠️ The SAN must match — a single wildcard is not enough

A served cert (default or otherwise) only avoids the browser warning if its SAN matches the
requested host, and a wildcard matches **exactly one** label:

| Wildcard SAN              | Covers `mypod.pods.tapis.io`? | Covers `mypod.pods.icicle.tapis.io`? |
| ------------------------- | :---------------------------: | :----------------------------------: |
| `*.pods.tapis.io`         |              ✅               |    ❌ (extra `icicle` label)         |
| `*.pods.icicle.tapis.io`  |              ❌               |               ✅                     |

Pod URLs are `<pod_id>.pods.<tenant>.<base>`, so you need a **per-tenant** wildcard
`*.pods.<tenant>.<base>`. The double wildcard `*.pods.*.<base>` is **not issuable** (CAs reject
multi-label wildcards) — which is exactly why per-pod certs were used originally.

## ✅ Recommended: per-tenant wildcards via DNS-01 (programmatic)

A per-tenant wildcard is a normal single-label wildcard and **is** issuable — but only via the
**DNS-01** challenge (HTTP-01/TLS-ALPN can't do wildcards). Done right this is fully automatic and
removes the per-pod issuance window entirely for standard domains.

### Behavioral tradeoff to know
Once a per-tenant wildcard is served as a **matching** cert, Traefik will use it for every
`*.pods.<tenant>.<base>` host and will **not** request a per-pod cert (it only invokes the resolver
when no cert matches the SNI). Traefik's `defaultCertificate` slot is a **single** cert per store,
so you cannot have per-tenant *defaults* — the wildcard must be a real served cert. Net effect:

- **Standard pod domains** → covered by the tenant wildcard. **No startup window, no holding page
  needed.** Per-pod ACME no longer fires for them (that's fine — the wildcard is valid).
- **Custom (BYOD) domains** → not covered by any wildcard, so keep per-domain ACME for those, and
  the in-app **PodLaunch** holding route remains their don't-break-the-page safety net.

### Static config (Traefik image) — add a DNS-01 resolver
```yaml
# traefik static config — needs your DNS provider + API creds (env vars)
certificatesResolvers:
  tlsletsencrypt-dns:
    acme:
      email: <ops-email>
      storage: /acme/acme.json        # persist on a volume so wildcards survive restarts
      dnsChallenge:
        provider: <your-dns-provider> # e.g. route53, cloudflare, rfc2136 …
        # delayBeforeCheck: 0
```
Mount `/acme` as a PVC (or reuse a secret-backed volume) so issued wildcards persist and renew
without re-hitting LE rate limits.

### Dynamic config — NOT implemented (reverted)
> **Status:** a flag-gated implementation of this (a `cert_wildcard_dns_resolver` knob that made
> `set_traefik_proxy` emit per-tenant wildcard "warmer" routers) was added and then **reverted**,
> because the extra Traefik-template branching caused a YAML-whitespace bug that took down all pod
> routing. The pods service is back to the original, proven per-domain ACME template. The notes
> below describe how to re-introduce it **carefully** if the per-pod window ever becomes worth it.

To make per-tenant wildcards programmatic, `set_traefik_proxy`
([health_central.py](../service/health_central.py)) would collect the unique `<tenant>.<base>`
domains across http pods and, for each, emit a sentinel "warmer" router carrying `tls.domains` so
Traefik obtains the wildcard via a DNS-01 resolver:
```yaml
routers:
  pods-wildcard-0:
    rule: "Host(`acme-warmer.pods.tacc.tapis.io`)"   # never matches real traffic
    entryPoints: [web]
    priority: 1
    service: pods-service
    tls:
      certResolver: <dns-resolver-name>
      domains:
        - main: "*.pods.tacc.tapis.io"
          sans: ["pods.tacc.tapis.io"]
```
http pod routers would also switch from `certResolver: tlsletsencrypt` to `tls: {}` (served from the
store). Traefik obtains each wildcard once, stores it in acme.json, serves it for all pods in that
tenant, and auto-renews.

> **If re-implementing:** the template change MUST be verified by rendering with **multiple** http
> pods (incl. one with auth-exclusions and one in splash mode) and parsing the result as YAML — a
> single-pod render will not catch router-key mis-indentation from Jinja whitespace control.

## ⚠️ Your constraint: Route53 managed upstream, no creds (HAProxy → nginx → Traefik)

You can't hand Traefik Route53 API creds, so Traefik can't run the Route53 DNS-01 challenge
directly. Two ways forward:

### Option A — CNAME-delegated DNS-01 (acme-dns) — keeps it automatic
Ask whoever controls Route53 to add a **one-time** CNAME per tenant:
```
_acme-challenge.pods.tacc.tapis.io  CNAME  <token>.auth.<your-acme-dns-host>
```
Run a small **acme-dns** server you *do* control; point Traefik's resolver at provider `acme-dns`
(lego supports CNAME delegation). Traefik then writes challenge records into the delegated zone you
own — **no Route53 creds needed** — and renews automatically (this would feed the DNS-01 resolver
the reverted feature described). This is the standard pattern for exactly your situation (a
CA-facing wildcard without giving out the main zone's API keys).

### Option B — out-of-band issuance + static mount — simplest, manual renewal
Have whoever controls Route53 issue the per-tenant wildcard(s) (or a single multi-SAN wildcard
covering every active `*.pods.<tenant>.<base>`) and hand you the cert/key. Mount them as static
certs in Traefik's static config:
```yaml
# traefik static config
tls:
  certificates:
    - certFile: /tmp/ssl/pods-tacc.crt
      keyFile:  /tmp/ssl/pods-tacc.key
    - certFile: /tmp/ssl/pods-dev.crt
      keyFile:  /tmp/ssl/pods-dev.key
```
Traefik prefers a SNI-matching cert from the store over per-domain ACME, so these wildcards cover
their tenants immediately. Downside: someone must re-issue/re-mount every ~90 days (or use a
longer-lived internal-CA cert).

## Relationship to the rest of the cert feature

- The health loop still records `cert_ready` / `cert_state` per domain
  ([health_central.py](../service/health_central.py)); the admin **System Health → TLS Certs
  (ACME)** panel surfaces whether ACME is actually issuing (e.g. it won't be, locally). With
  per-tenant wildcards, standard domains report `ready` immediately.
- The in-app **PodLaunch** holding route remains the fix for **custom (BYOD) domains**, which a
  wildcard can't cover.

## Alternative if you want to keep per-pod certs

If per-pod certs must stay, the only way to also cover the window is **pre-warming**: trigger
issuance before the user arrives. The health loop's `_probe_cert_ready` TLS handshake already
does this incidentally (the first handshake to a certless host kicks off ACME), so by the time a
user clicks, the per-pod cert is often already issued — but a brief window can remain. A per-tenant
wildcard is the only way to fully eliminate it.

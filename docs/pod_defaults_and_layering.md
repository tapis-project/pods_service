# Pod Defaults, Provenance, and Template Layering

> Why a derived redis pod shows `http:5000` in the diff view even though its
> template sets `tcp:6379`, and what we can do about the broader "too many
> default fields" problem.

## TL;DR

- A Pod object, once serialized with `.dict()`, has **every field populated with
  a default** — there is no signal distinguishing "user chose this" from "this is
  the Pydantic default."
- We already have a provenance ledger: **`modified_fields`**, computed at create
  time from `new_pod.dict(exclude_unset=True)`. The template-merge engine trusts
  it and produces correct derived pods.
- The bug users see ("member `http:5000` in use / template `tcp:6379` replaced")
  is a **display/diff** problem: `modified_fields` is popped out of `display()`,
  so the frontend compares a *persisted default* against the template and labels
  it "replaced."
- **Recommended fix: surface `modified_fields` (provenance) to the client** so the
  UI can mute/hide defaults and suppress false "replaced" diffs. The deeper "store
  no defaults at all" refactor is the north star but high blast radius.

---

## 1. Where defaults come from (three stacked layers)

Take `networking` as the canonical example.

1. **Top-level field default** — `service/models_pods.py` (`PodBase.networking`):
   ```python
   networking: Dict[str, Networking] = Field(
       {"default": {"protocol": "http", "port": 5000}}, ...)
   ```
   If the user omits `networking` entirely, this whole object is supplied.

2. **Nested model default** — `service/models_pods.py` (`Networking`):
   ```python
   protocol: str = Field("http", ...)
   port: int     = Field(5000, ...)
   ```
   A *partial* `{"default": {"port": 6379}}` still gets `protocol` back-filled to
   `"http"`, plus ~30 other fields (`cors_*`, `tapis_auth_*`, `proxy_compression_*`)
   filled with their own defaults.

3. **Serialization** — `pod.dict()` / `display()` emit **all** fields. Pydantic
   does not, by default, tell you which were set vs defaulted.

The same pattern repeats for `resources` (a `Resources` model with cpu/mem/gpu
defaults), `time_to_stop_default=43200`, `compute_queue="default"`,
`ready_condition="available"`, etc. The result: a one-line `{image, template}`
request round-trips as a ~60-field object, almost all of it noise.

---

## 2. The provenance ledger we already have: `modified_fields`

At create time — `service/api_pods.py` (`create_pod`):

```python
pod = Pod(**new_pod.dict())                 # (!) includes the networking DEFAULT
for arg in new_pod.dict(exclude_unset=True).keys():   # the TRUTH: only user-set fields
    if arg == "resources":
        for sub_arg in new_pod.resources.dict(exclude_unset=True).keys():
            pod.modified_fields.append(f"resources.{sub_arg}")
    else:
        pod.modified_fields.append(arg)
```

`exclude_unset=True` is Pydantic's `__fields_set__` — it knows the user only set
`image` and `template`. So `modified_fields == ["image", "template"]`.

**But note the contradiction now stored on the pod:**

| field          | stored value                                   | reflects user intent? |
|----------------|------------------------------------------------|-----------------------|
| `networking`   | `{"default": {"protocol":"http","port":5000}}` | **No** — persisted default |
| `modified_fields` | `["image", "template"]`                     | Yes |

The `networking` default got persisted because `new_pod.dict()` (without
`exclude_unset`) materialized it. `modified_fields` correctly omits it.

---

## 3. Why the *derived* pod is correct but the *diff view* is wrong

### Derive trusts `modified_fields` (correct)

`service/models_templates_utils.py` (`combine_pod_and_template_recursively`,
networking branch):

```python
merged_network = network_def.copy()          # template's def, e.g. tcp:6379
if network_name in final_network_obj and "networking" in input_obj_modified_fields:
    merged_network.update(final_network_obj[network_name])   # only if user set networking
```

Because `networking` is **not** in `modified_fields`, the pod's persisted
`http:5000` is *not* allowed to override the template. Final derived networking =
`tcp:6379`. Correct.

### Display/diff does NOT have `modified_fields` (wrong)

`display()` pops it out — `service/models_pods.py`:

```python
display.pop('modified_fields')
```

So the frontend receives a pod whose raw `networking` is `http:5000`, with no way
to know that value is an un-chosen default. The diff/layering UI then compares
"pod member networking (`http:5000`)" against "template networking (`tcp:6379`)"
and renders **"member http:5000 in use → template tcp:6379 replaced."**

It is a false positive: there was never a user choice to replace.

---

## 4. Options

### A. Sparse storage — the "true" fix (high risk)

Persist only user-set fields; apply defaults only when computing the *effective*
pod (derive / k8s spec). Stored pod carries **no** defaults.

- Pros: removes the problem at the root; objects returned are genuinely minimal.
- Cons: the whole codebase assumes `pod.networking`, `pod.resources`, etc. are
  always populated (k8s spec generation, health loop, traffic, validators). Every
  reader must go through a "resolve defaults" accessor. Multi-week refactor.

### B. Surface provenance — recommended (low risk)

Stop hiding `modified_fields`; include it (or a derived `field_provenance` map) in
the read model. The UI uses it to:
- render defaulted fields muted / collapsed ("inherited default"),
- suppress "replaced" in the diff when the member side is not in `modified_fields`
  (treat template value as the effective one with no conflict).

- Pros: fixes the reported symptom directly; the merge engine already trusts this
  exact data, so we're just exposing the truth we already compute. Near-zero
  backend risk.
- Cons: doesn't shrink the wire payload; defaults still travel, just labeled.

### C. Don't persist defaults at creation (medium risk)

`Pod(**new_pod.dict(exclude_unset=True))` so the stored `networking` is absent when
unset; re-apply defaults on read via a resolver.

- Pros: removes the persisted `http:5000`; smaller stored rows.
- Cons: readers expecting populated fields break; **nested** dict defaults still
  leak (a partial `Networking` still back-fills `protocol`), so this only solves
  the top-level case. Also changes DB-shape assumptions.

---

## 5. Recommendation

**Do B now; treat A as the north star.**

`modified_fields` is already the source of truth the merge engine relies on — it is
simply being withheld from the client. Surfacing it is the minimal, correct fix for
the "replaced" false positive and gives the UI everything it needs to de-clutter
default-laden objects (gray them out, collapse them, or hide behind a "show
defaults" toggle).

Pursue A only when we're ready to route every default-reader through a single
`effective_pod()` resolver — at which point both the wire payload and the stored
row become genuinely sparse.

### Concrete B sketch

1. **Backend** — in `PodBaseFull.display()`, stop popping `modified_fields` (or add
   a sibling `field_provenance: {field: "user" | "template" | "default"}`).
   - `user`   → field in `modified_fields`
   - `template`→ field supplied by the resolved template chain
   - `default`→ neither (pure Pydantic default)
2. **Frontend (tapis-ui)** — in the detail view and the StackUpdate/diff modal:
   - render `default`-provenance fields muted and collapsed by default,
   - in diffs, when the member side is `default`-provenance, show the template
     value as *the* value (no "replaced" badge / no conflict).
3. **Nested fields** — extend provenance to dotted paths (`resources.cpu_request`,
   `networking.default.port`) using the same `exclude_unset` recursion already
   used for `resources.*` in `create_pod`.

---

## 6. Gotchas to remember

- **`new_pod.dict()` vs `new_pod.dict(exclude_unset=True)`** — the former
  materializes defaults (and is what gets persisted); the latter is the provenance
  signal. Don't conflate them.
- **Nested defaults survive `exclude_unset`** — a partial networking/resources dict
  still back-fills inner model defaults. Top-level sparseness ≠ deep sparseness.
- **The merge engine is already correct** — do not "fix" derivation; the divergence
  is purely display-side. Any change should make the *view* match what derive
  already computes.
- **`modified_fields` is top-level + `resources.*` granularity today** — extending
  provenance to `networking.default.*` requires the same per-subfield
  `exclude_unset` walk.

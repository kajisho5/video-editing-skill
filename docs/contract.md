# Contract: source of truth, versioning, drift

`video-editing contract --json` is generated from the code (`operations.py`, `errors.py`, `compiler.py`,
`ffmpeg_skill.py`, `paths.py`), never maintained beside it. `contract --check` proves that the document and the
implementation agree, and `tests/contract/contract.json` is a golden copy that CI compares against the live document
so that no field an agent keys on changes without a deliberate regeneration.

## Pinned blocks (breaking when changed)

Agents pin a snapshot of the contract and compare these blocks **verbatim** (video-production-agent's adapter does so
in its integration test):

```
schema, skill_id, contract_version, operations, unsupported, errors, execution, capabilities, schemas
```

`contract_version` (docs/decisions.md ADR-007), not `version`, is the pinned identity field: it is the version of
this *shape*, separate from `version` (this package's own release version, free to change on any release — a
dependent pins a range against `contract_version`, never `version`). `version` still appears at the top level and
in every response's `skill` block (it names which release produced a given document), but a `version`-only change
is additive drift, not breaking.

plus, per ToolSpec in `tools[]`: `tool_id, skill_id, version, operation_type, capability, required_capabilities,
inputs, input_arity, produces_output, deterministic, result_keys, executed_by, kind`, and the list of tool ids itself.
video-production-agent's adapter additionally compares `request_shape`, `response_shape`, `engine`, `formats`,
`capability_names` and `tools[].parameters` / `writes_media` verbatim (`versioning.also_pinned_by_agents`), so those
are treated as pinned too: an optional request field such as `outputs[].encoding` is documented in its own block
(`contract.encoding.request_field`) and folded into `request_shape` only with the next version.

A change inside any of them is a **breaking contract change**:

- bump `contract_version` (this package does not yet release independently of its contract, so bump the skill
  version too — while the major version is 0: the minor version, e.g. `0.1.x → 0.2.0`);
- expect every agent to re-pin its snapshot and to review its lowering (parameter names, types, error mapping);
- regenerate `tests/contract/contract.json` in the same change.

Examples of breaking changes: a new operation type or parameter (`operations` changes), a renamed parameter, a new
or removed error code, a changed exit code or retryable default, a new capability name, a changed schema id, a new
CLI flag in `canonical_invocation`.

## Additive blocks (allowed within a version)

Everything outside the pinned blocks may be added or extended without a version bump:

- new top-level keys (`media_compatibility`, `graph`, `validation`, `doctor_shape`, `versioning`, `provides`, …);
- new keys inside a `tools[]` entry (`media`, …);
- new keys in a response document (`execution.sources`, `execution.reused`, `execution.request_sha256`,
  `execution.engine`, `outputs[].container / reused / operation_id`, records with `status: skipped`, …). The
  response schema id stays `video-editing/response@1` because every previously documented key keeps its meaning.

Removing a key, or changing the meaning of an existing one, is breaking even outside the pinned blocks.

## Drift detection

`contract --check tests/contract/contract.json` (also `--check -` with the document on stdin) reports, and exits 1 on:

- `implementation`: the live document disagrees with the code (operation allowlist, compiler flags, capabilities,
  unsupported list, error table, execution guarantees, media table, versioning block, README / SKILL.md mentions);
- `drift`: the saved copy differs from the live document. Every difference is classified:
  - `[breaking]` — a pinned block or pinned ToolSpec field changed, a key or tool was removed, the request shape changed;
  - `[additive]` — a key was added outside the pinned blocks, a non-pinned block changed.

  `drift.compatibility` is `none`, `additive` or `breaking`. Any drift fails the check: the golden copy is
  regenerated on purpose (`video-editing contract --json > tests/contract/contract.json`), reviewed in the same
  change, and the classification tells the reviewer whether agents must re-pin.

## 0.2.0 (planned; docs/decisions.md ADR-002 / ADR-003 / ADR-004)

`versioning.next["0.2.0"]` lists the breaking changes queued for the next version: `RESIZE.height` (exactly one of
width / height), `outputs[].encoding` in `request_shape`, `CROP` and `IMAGE_INSERT` once ffmpeg-skill ships typed
tools for them. `FREEZE`, `REVERSE` and `POSITION` are not planned. Until then 0.1.x grows only additively:
`media_compatibility`, `media_policy`, `frame_semantics`, `encoding`, `graph`, `validation`, `doctor_shape`,
`versioning`, `tools[].media`, and the richer execution report.

## `provides` (docs/decisions.md ADR-006)

`provides` lists this Skill's eight operations by their cross-repository Capability id (`video.trim`, `video.cut`,
`video.concat`, `video.speed`, `video.fit`, `video.fill`, `video.resize`, `video.overlay` — the same `capability`
string `operations.OPERATIONS` and `tools[].capability` already carry), each with its `tool_id` and a `lifecycle`.
It exists for `kajisho5/AI-video-production-OS`'s `CapabilityContract.provides` (`docs/SPEC.md` there), so a
registry can resolve "who provides `video.trim`" without hardcoding this repository. It is additive, not pinned,
and derived from `OPERATIONS` — it cannot say anything `tools[]` doesn't already say, only index it differently.

## What an agent may rely on

- `tools[].tool_id` = `video-editing/<type lower-case>`; `operations[<TYPE>].parameters` names every accepted
  parameter (unknown ones are refused); `tools[].media.requires` says what the inputs must be.
- `errors.codes` / `exit_codes` / `retryable_default`: the only codes a response can carry.
- `execution.canonical_invocation`: `video-editing run - --json --workspace <dir> --allowed-input <root>`; the
  workspace, the allowed roots and the ffmpeg-skill location are CLI flags, never request fields.
- `response_shape` and `validation.response`: every document is self-checked before it is printed
  (`response.check_response`); a document that would violate the shape is reported as `INTERNAL_ERROR` instead.
- `doctor_shape`: `doctor --json` is the capability-discovery endpoint; `operations[].status` is `AVAILABLE` only
  when the tool and every encoder / filter it needs are present, `supported_operations` lists exactly those.

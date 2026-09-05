# Contract: source of truth, versioning, drift

`video-editing contract --json` is generated from the code (`operations.py`, `errors.py`, `compiler.py`,
`ffmpeg_skill.py`, `paths.py`), never maintained beside it. `contract --check` proves that the document and the
implementation agree, and `tests/contract/contract.json` is a golden copy that CI compares against the live document
so that no field an agent keys on changes without a deliberate regeneration.

## Pinned blocks (breaking when changed)

Agents pin a snapshot of the contract and compare these blocks **verbatim** (video-production-agent's adapter does so
in its integration test):

```
schema, skill_id, version, operations, unsupported, errors, execution, capabilities, schemas
```

plus, per ToolSpec in `tools[]`: `tool_id, skill_id, version, operation_type, capability, required_capabilities,
inputs, input_arity, produces_output, deterministic, result_keys, executed_by, kind`, and the list of tool ids itself.

A change inside any of them is a **breaking contract change**:

- bump the skill version (while the major version is 0: the minor version, e.g. `0.1.x → 0.2.0`);
- expect every agent to re-pin its snapshot and to review its lowering (parameter names, types, error mapping);
- regenerate `tests/contract/contract.json` in the same change.

Examples of breaking changes: a new operation type or parameter (`operations` changes), a renamed parameter, a new
or removed error code, a changed exit code or retryable default, a new capability name, a changed schema id, a new
CLI flag in `canonical_invocation`.

## Additive blocks (allowed within a version)

Everything outside the pinned blocks may be added or extended without a version bump:

- new top-level keys (`media_compatibility`, `graph`, `validation`, `doctor_shape`, `versioning`, …);
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

# Security boundary

video-editing-skill is designed to be driven by an untrusted producer of requests (an agent, an LLM). The
request can describe an edit; it cannot describe how to run anything.

## What the request cannot do

| Attempt | Result |
|---|---|
| `command`, `cmd`, `argv`, `args`, `shell`, `exec`, `executable`, `script`, `filter`, `filter_complex`, `ffmpeg`, `binary`, `env` anywhere in the document | `INVALID_REQUEST` (reason `forbidden_key`) before anything else is looked at |
| any key outside the schema, any operation type outside the allowlist | `INVALID_REQUEST` / `UNSUPPORTED_OPERATION` |
| a value that reaches an ffmpeg filter graph (pad colour, transition, position, aspect) | closed vocabularies or integers only; free strings are refused |
| an output path that is absolute, contains `..`, leaves the workspace through a symlinked parent, equals an input, or already exists (without `options.overwrite`) | `PATH_NOT_ALLOWED` |
| an input outside the allowed roots, reached by `..` or by a symlink whose target is outside | `PATH_NOT_ALLOWED` with reason `traversal` / `outside_allowed_roots` / `symlink_escape` |
| Windows reserved device names (`CON`, `NUL`, `COM1`…), `<>:"|?*`, control characters, trailing dots / spaces in any component | `PATH_NOT_ALLOWED` (reason `reserved_name`), on every platform |
| a still image or broken container declared as a video source | `INVALID_INPUT` (reason `no_duration`) at probe time, before any encode |
| a request over 4 MiB, more than 200 sources / 500 operations / 50 outputs | `INVALID_REQUEST` |
| choosing the workspace, the allowed roots or the ffmpeg-skill location | not request fields: CLI flags / environment only |

## How media is run

- The only process launcher is `ffmpeg_skill.py`: `[sys.executable, <ffmpeg-skill>/scripts/<tool>.py, typed argv, --json]`.
  `tests/test_security.py` walks the AST of every module to prove no other `subprocess` call, no `os.system`,
  `shell=True`, `eval`, `exec`, `importlib` or network import exists.
- Every flag is checked against `compiler.ALLOWED_FLAGS` per tool. Paths appear only as positionals, after `-o`
  and after `--image`. A value starting with `-` is passed as `--flag=value` so it can never be read as an option.
- ffmpeg and ffprobe are resolved by ffmpeg-skill from `PATH`. ffmpeg-skill is located from
  `VIDEO_EDITING_FFMPEG_SKILL_DIR` / `--ffmpeg-skill-dir` / fixed default locations; a directory qualifies only
  when it has `scripts/probe.py`, and its version must be in the supported range.
- Children run in their own process group with `stdin=DEVNULL` and a scrubbed environment (PATH, HOME, TMP,
  locale, Windows system variables, `PYTHONUTF8`). A timeout or SIGINT / SIGTERM kills the whole group.
- Tools write to `<workspace>/.video-editing/work/<operation_id>.partial.<pid>.<ext>`. A file is renamed to its
  final name only after validation (probe: video stream, duration, frame size; expected duration; sha256).
  Failed partials are deleted. Final outputs are copied to their path via `.partial` + atomic rename.

## What is not guaranteed

- The check-then-open window on the final path component (a symlink swapped between validation and the read)
  is not closed; the resolved path, not the raw one, is what is passed on.
- The inputs are readable by whoever runs the skill; allowed roots limit which files a *request* may name, not
  what the process could read.
- Reuse records in the work directory are trusted if their idempotency key and hash match; a writer with access
  to the workspace can plant one. The work directory is inside the workspace by design.
- ffmpeg-skill itself has no path policy and passes `-y`; this skill never hands it an input path as an output.

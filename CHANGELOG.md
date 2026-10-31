# Changelog

## Before release automation (0.1.0 - 0.4.0, never tagged)

Later entries are written above this one by the release workflow, from the commits merged since the previous tag.

- 0.4.0: `SPEED.smooth` (`blend` / `interpolate`, ffmpeg-skill `fit.py --smooth`), ADR-012; then, unreleased on top
  of 0.4.0: no ffmpeg-skill version window (capability detection, ADR-013), `FREEZE` / `REVERSE` and an
  engine-neutral `unsupported` list (ADR-014), `IMAGE_INSERT` (ADR-015)
- 0.3.0: `ROTATE` (ffmpeg-skill `fit.py --rotate` / `--flip`), ADR-011
- 0.2.0: `FILL.anchor`, `outputs[].encoding` in the request shape (ADR-009); `OVERLAY` on audio-less video (ADR-010)
- 0.1.0: the typed edit graph (TRIM, CUT, CONCAT, SPEED, FIT, FILL, RESIZE, OVERLAY), contract, doctor, provenance,
  reuse, `provides` (ADR-006), `contract_version` (ADR-007), `dependencies` (ADR-008)

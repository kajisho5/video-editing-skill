# Changelog

## v0.4.0 - 2026-10-31

- IMAGE_INSERT via ffmpeg-skill/insert; CHANGELOG, release workflow, dependabot (roadmap 4, 6) (#21) (f6e13a9)
- FREEZE and REVERSE via ffmpeg-skill; unsupported list from engine detection (roadmap 1-3) (#20) (3d65981)
- Add ECOSYSTEM_ROADMAP.md: gaps, wiring and PR-sized steps from the 2026-10-30 ecosystem audit (#19) (9616d56)
- Add the Sponsor badge and .github/FUNDING.yml (#18) (9d666e0)
- ci: keep CI green on Ubuntu 26.04 (Python 3.9, ffmpeg 8.0) (#17) (e580bf4)
- fix: align pyproject version with VERSION (0.4.0) (82f05c7)
- docs(readme): fresnel-style header with a Liquid Glass app icon (#15) (2656d9e)
- fix: work with any ffmpeg-skill version (capability detection, no version window) (#16) (4a6f7a4)
- Add SPEED.smooth (judder reduction via ffmpeg-skill/fit --smooth) (#14) (895f8b7)
- Add ROTATE operation (--rotate/--flip via ffmpeg-skill/fit) (#12) (9c82c72)
- OVERLAY no longer requires audio; bump SUPPORTED_MIN to ffmpeg-skill 0.10.0 (8faa726)
- Release 0.2.0: FILL.anchor and outputs[].encoding formalized in request_shape (5aeaee6)
- Verify ffmpeg-skill 0.10.0; correct RESIZE.height's documented status; flag FILL.anchor (c0322ea)
- Update CLAUDE.md status snapshot: PR #4 and PR #5 merged (8bad78f)
- Add dependencies: publish the ffmpeg-skill version range already enforced at runtime (7984029)
- Add CLAUDE.md: durable maintainer state for a session with no conversation memory (453d1dc)
- Add contract_version: a shape-version axis independent of the skill's release version (a751c7b)
- Add provides: publish Capability ids for cross-repository discovery (8b09041)
- Audit fixes: CONCAT single-dimension frame rule, odd-frame refusal for frame-keeping operations, engine-version and fixture notes (8a5e6c9)
- Frame semantics, typed encoding profile, media policy, real-media matrix (contract 0.1.0 additive; 0.2.0 designed in ADR-002) (94fd142)
- Production completeness: media compatibility, engine gaps, output validation, reuse re-validation, response self-check, doctor as capability discovery (347cf84)
- CI fixes: ffmpeg facts via ffmpeg-skill doctor, '.' path component, escape classification, ruff rule set (22161fa)
- Contract drift check, engine error-contract tests, CI on pull requests (b70de14)
- Implement video-editing-skill: typed edit requests executed through ffmpeg-skill (2c0619b)
- Initial commit (6438667)

## Before release automation (0.1.0 - 0.4.0, never tagged)

Later entries are written above this one by the release workflow, from the commits merged since the previous tag.

- 0.4.0: `SPEED.smooth` (`blend` / `interpolate`, ffmpeg-skill `fit.py --smooth`), ADR-012; then, unreleased on top
  of 0.4.0: no ffmpeg-skill version window (capability detection, ADR-013), `FREEZE` / `REVERSE` and an
  engine-neutral `unsupported` list (ADR-014), `IMAGE_INSERT` (ADR-015)
- 0.3.0: `ROTATE` (ffmpeg-skill `fit.py --rotate` / `--flip`), ADR-011
- 0.2.0: `FILL.anchor`, `outputs[].encoding` in the request shape (ADR-009); `OVERLAY` on audio-less video (ADR-010)
- 0.1.0: the typed edit graph (TRIM, CUT, CONCAT, SPEED, FIT, FILL, RESIZE, OVERLAY), contract, doctor, provenance,
  reuse, `provides` (ADR-006), `contract_version` (ADR-007), `dependencies` (ADR-008)

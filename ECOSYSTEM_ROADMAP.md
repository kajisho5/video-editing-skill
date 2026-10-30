# Roadmap — video-editing-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **映像 (Visual)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

v0.4.0, Python 3.9+ stdlib. TRIM, CUT, CONCAT (+transition), SPEED, FIT, FILL, RESIZE, OVERLAY, ROTATE. 171 tests, CI on 3 OSes x py3.9/3.11 against ffmpeg-skill pinned + main. Best OS-schema fit of all skills (contract_version 4.0, dependencies declared).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| カット・連結 | done |  |
| 尺・画角合わせ | done |  |
| トランジション | done | CONCAT transition |
| 静止画オーバーレイ | done |  |

## Wiring

Agent wires cut (2nd candidate), concat, speed, resize, fit, fill, overlay. Not wired: trim (declared phase 2), rotate. Delegates to ffmpeg-skill probe, cut, join, fit, overlay.

## Steps

- [x] **1. Unsupported list from engine detection** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20) — CROP / FREEZE / REVERSE / IMAGE_INSERT are listed as impossible, but ffmpeg-skill now ships crop, freeze, reverse, insert
  - Done when: the contract does not call a tool missing when the detected checkout has it
- [x] **2. FREEZE via ffmpeg-skill/freeze** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20)
  - Done when: integration test; provides gains video.freeze
- [x] **3. REVERSE via ffmpeg-skill/reverse** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20)
  - Done when: integration test
- [ ] **4. IMAGE_INSERT via ffmpeg-skill/insert** — a still can join CONCAT
  - Done when: integration test
- [ ] **5. CROP: implement or leave to the agent** — ADR either way (the agent already calls ffmpeg-skill/crop)
  - Done when: no duplicate ownership
- [ ] **6. CHANGELOG, release workflow, dependabot** — align with sibling repos
  - Done when: a tag produces a release

## Boundaries

Still-image overlay also exists in motion-graphics `image_overlay`; ecosystem.json gives it to video-editing.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).

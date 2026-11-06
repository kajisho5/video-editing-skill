# Roadmap — video-editing-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **映像 (Visual)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

v0.4.4, Python 3.9+ stdlib. TRIM, CUT, CONCAT (+transition), SPEED, FIT, FILL, RESIZE, OVERLAY, ROTATE, FREEZE, REVERSE, IMAGE_INSERT, CROP. 171 tests, CI on 3 OSes x py3.9/3.11 against ffmpeg-skill pinned + main. Best OS-schema fit of all skills (contract_version 4.0, dependencies declared).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| カット・連結 | done |  |
| 尺・画角合わせ | done | pixel-rectangle CROP included ([#25](https://github.com/kajisho5/video-editing-skill/pull/25), ADR-016) |
| トランジション | done | CONCAT transition |
| 静止画オーバーレイ | done |  |

## Wiring

Agent wires cut (2nd candidate), concat, speed, resize, fit, fill, overlay, trim and rotate (video-production-agent#80), image_insert (video-production-agent#86) and crop (video-production-agent#94); FREEZE / REVERSE are lowered but not planned (agent step 17). Pinned at 0.4.2. Delegates to ffmpeg-skill probe, cut, join, fit, overlay.

## Steps

- [x] **1. Unsupported list from engine detection** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20) — CROP / FREEZE / REVERSE / IMAGE_INSERT are listed as impossible, but ffmpeg-skill now ships crop, freeze, reverse, insert
  - Done when: the contract does not call a tool missing when the detected checkout has it
- [x] **2. FREEZE via ffmpeg-skill/freeze** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20)
  - Done when: integration test; provides gains video.freeze
- [x] **3. REVERSE via ffmpeg-skill/reverse** — [#20](https://github.com/kajisho5/video-editing-skill/pull/20)
  - Done when: integration test
- [x] **4. IMAGE_INSERT via ffmpeg-skill/insert** — [#21](https://github.com/kajisho5/video-editing-skill/pull/21) — a still can join CONCAT
  - Done when: integration test
- [x] **5. CROP: implement or leave to the agent** — ADR either way (the agent already calls ffmpeg-skill/crop) — implemented here: [#25](https://github.com/kajisho5/video-editing-skill/pull/25), ADR-016
  - Done when: no duplicate ownership
- [x] **6. CHANGELOG, release workflow, dependabot** — [#21](https://github.com/kajisho5/video-editing-skill/pull/21) — align with sibling repos
  - Done when: a tag produces a release
- [x] **7. ffmpeg-skill 3.0.0** — [#26](https://github.com/kajisho5/video-editing-skill/pull/26) — cut.py's new defaults checked against TRIM / CUT; ADR-017
  - Done when: the real-media suite passes against 3.0.0 and 2.x in CI
- [x] **8. Segment cache and partial re-render** — CUT / CONCAT keep a cache of encoded segments keyed by the source hash, the range and the encode settings; after a change only the segments that changed are encoded and the rest are joined by stream copy. Running the same request twice writes nothing new. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - Why: fixing one cut in a long programme takes minutes, not a full re-encode（なぜ要るか：長尺の1カットだけ直すとき全体を書き出し直さない）
  - Done when: a 1-hour fixture with one changed cut re-encodes only that segment (counted), and the result equals a full render (framemd5)
  - Done (ADR-018): `options.segment_cache` — a frame-precision CUT is cut range by range into a content-keyed cache and joined by `join.py --copy`; `SegmentCacheLongFormTests` (1-hour source, 3 cuts) re-encodes 1 part after one change (`encoded: 1, reused: 2`) and matches an empty-cache render by framemd5; a repeated request is `reused`. Needs ffmpeg-skill 2.24.0+ (`--copy`), refused before execution without it. CONCAT's own join still re-encodes (it conforms size, rate and stereo audio); its partial re-render comes from its inputs
- [ ] **9. Keyframe-aware long-form cuts** — TRIM / CUT on 2-8 h sources choose a stream copy when the in and out fall on keyframes, and re-encode only the GOPs at the boundaries otherwise (smart render), reported per cut
  - Why: trimming the head and tail of an 8-hour recording needs no re-encode（なぜ要るか：長尺の頭と尻を落とすだけなら再エンコード不要）
  - Needs: Velisa ROADMAP, Proposals to ffmpeg-skill 1 (cut-point candidates); until then whole re-encode as today
  - Done when: a fixture cut on keyframes is byte-for-byte a stream copy; one cut between keyframes re-encodes at most two GOPs
  - Partly done (ADR-019): `precision: auto` (cut.py lossless mode, `--tolerance 0.2`); `KeyframeAwareCutTests` shows a cut on keyframes is the source's packets byte for byte, and `execution.operations[].cuts` reports copy / re-encode per cut. **Not met:** a cut between keyframes is re-encoded whole, not at most two GOPs. That needs the engine to re-encode only the boundary GOPs and splice them into copied packets (proposal 1 plus a smart-render mode in ffmpeg-skill); doing it here would mean running ffmpeg outside ffmpeg-skill. Left unchecked until the engine offers it
- [x] **10. Punch-in on the speaker** — PUNCH_IN: a zoom to a stated scale centred on detection-skill's face box for a range (a hard cut in, or a short ease), kept inside the frame; used at a share of jump cuts
  - Why: the common punch-in on the speaker follows the face automatically（なぜ要るか：話者に寄る演出を顔の位置に合わせて入れられる）
  - Done when: a fixture with a face left of centre keeps the face inside the zoomed frame for the whole range
  - Done (ADR-020): `PUNCH_IN {start, end, scale, subject}` — window centred on the caller's box (detection-skill's face box) and kept inside the frame; `PunchInTests` checks decoded frames across the range (the doubled box stays fully inside). Hard cut only: a short ease needs a time-varying crop (Velisa proposal 2 to ffmpeg-skill). Which jump cuts get one stays the agent's decision (ADR-001)
- [x] **11. One edit, several aspects** — one cut list rendered as 16:9, 9:16 and 1:1 in one request, each shot cropped with one window per shot from detection-skill's subject path (CROP), the audio shared
  - Why: the landscape programme and the vertical shorts ship from one edit（なぜ要るか：横の本編と縦型を同じ編集から同時に納品できる）
  - Done when: a three-shot fixture gives three outputs of equal duration, each shot's window inside the source
  - Done (ADR-021): a request graph, no new type — shared TRIMs, one CROP per shot per aspect (windows from the caller), one CONCAT per aspect; `examples/multi-aspect.json`. `MultiAspectTests`: 16:9 / 9:16 / 1:1 outputs equal in duration to within a frame, every window checked inside its measured shot, one outside refused before execution. The sound is the same source ranges in each output but encoded per output (not bit-identical); one shared track would need a mux step
- [x] **12. AI-generated clips in an edit** — a generated clip is normalised before it joins (fps, size, SAR, colour tags, a silent track when it has none), and provenance marks it `AI_GENERATED` with its generator and licence as declared by the caller
  - Why: generated clips join an edit without format mismatches or a missing AI label（なぜ要るか：生成映像を混ぜても規格不一致と表記漏れが起きない）
  - Done when: a 24 fps, untagged, silent fixture joins a 29.97 fps edit without a duration drift and carries the mark
  - Done (ADR-022): `sources[].ai_generated {generator, licence}` (declared by the caller) marks every operation and output containing it (`marks: [AI_GENERATED]`, provenance DECLARED); CONCAT conforms size, rate, square pixels, BT.709 tags and adds silence. `GeneratedClipTests`: a 24 fps 4:3-pixel untagged silent clip joins 29.97 fps clips with duration and frame count equal to the sum to within a frame, and carries the mark. Engine finding: join.py writes 30000/1001 as 2997/100 (proposal to ffmpeg-skill)
- [ ] **13. Stinger transition** — CONCAT gains `transition: stinger`: a stinger file with alpha (hyperframes-skill, remotion-ffmpeg-skill or blender-skill output, with its `stinger.json`) is laid over the join, and the outgoing clip switches to the incoming one at the stinger's cut point, the frame where the graphic covers the whole picture; the stinger's audio is mixed in. The timeline reports the cut at that frame. A stinger without a stated cut point, or that does not cover the frame there, is refused
  - Why: logo wipes and graphic transitions between scenes are common in event and YouTube edits, and the switch must happen under full cover（なぜ要るか：スティンガーを挟み、画面が完全に覆われた瞬間に前後のクリップを切り替えられる）
  - Needs: a stinger with a cut point (hyperframes-skill stinger templates, or any alpha file with its cut point given)
  - Done when: two clips joined with a 1.2 s stinger cut at 0.5 s switch at exactly that frame (framemd5 around the cut), and qc-skill's stinger cut-point check passes on the result
  - Partly done (ADR-023): CONCAT `transition: {type: stinger, stinger, cut_point, cover}`; `StingerTests` shows the switch at exactly the cut frame (frame 60 fully covered, 59 shows A, 61 shows B, decoded pixels), the stinger's sound mixed in, the timeline reporting the cut there, and a cut point outside the stated cover refused. **Not met:** qc-skill has no stinger cut-point check yet (to run on the result), and full cover is checked against the stated cover window only — no ffmpeg-skill tool measures a stinger's alpha at one frame. Left unchecked until qc-skill's check exists
- [x] **14. Retake and stumble proposals** — from a transcript with word timings, find retakes (a phrase said again), stumbles (broken-off words, false starts), repeats and hesitation words, and propose each as a cut with its reason; nothing is applied, only proposals a person approves reach the edit (CONDENSE, step 16). English by default; other languages as selectable regional profiles
  - Why: the usual clean-up of a talk or interview is proposed, not done blind（なぜ要るか：言い直し・詰まりを理由付きで提案し、人が承認したものだけ切る）
  - Done when: every retake in a fixture is proposed, and a clean-speech fixture gives no more false positives than the default threshold allows
  - Done (ADR-024): `video-editing propose` with `retakes: {transcript, profile, min_confidence}` returns proposals (repeat / restart / fragment / filler) with a reason and a rule confidence, `applied: false`, writing nothing. `RetakeProposalTests`: all six retakes of the English fixture and all four of the Japanese one (`profile: ja`) are proposed; the clean-speech fixture gives none at the default threshold (its emphatic doubles score 0.3, below 0.5)
- [x] **15. Cuts by amount of motion** — find the spans with no motion (a fixed camera with nobody in frame) from the picture, and combine them with the sound (silence) by and / or into labelled spans
  - Why: dead spans of a fixed-camera recording are found without watching it（なぜ要るか：定点カメラの不在区間を映像から見つける）
  - Done when: a fixture with a still span finds it to within one frame
  - Done (ADR-025): `propose` with `source`, `static: {}` and / or `silence: {threshold_db, min_seconds}`, `combine: {mode: and | or}` returns labelled spans (`static`, `silence`, `silence+static`) as proposals; the static spans are the shots ffmpeg-skill `scenes --shots` labels static, the silences `silence --list`. `MotionSpanTests`: a 3 s still span between moving ones is found at 2.0-5.0 s to within one frame; `and` with a 2.5-4.5 s silence gives 2.5-4.5, `or` gives 2.0-5.0 labelled silence+static. A still span with no visible change of picture at its edges is found only at the shot edges the engine detects
- [x] **16. A choice per labelled span** — per label (silence, static, retake, ...), cut / keep / speed (with a factor)
  - Why: silences can be sped up instead of cut, retakes cut, the rest kept, in one edit（なぜ要るか：ラベルごとに切る・残す・早送りを選べる）
  - Done when: an edit that keeps the silences at double speed has the right duration and keeps the pitch (atempo)
  - Done (ADR-026): `CONDENSE {segments: [{start, end, label}], rules: {label: cut | keep | {speed: f}}, default}`; each kept piece cut frame-accurately, a sped one retimed by fit.py `--duration --method speed` (atempo), joined in order. `CondenseTests`: a silence proposed by `propose`, approved and kept at 2x makes 6 s into 5 s (within 0.1 s), and the quiet 880 Hz tone in it is still 880 Hz (not 1760); the timeline shows speeds 1, 2, 1
- [x] **17. Review of the planned cuts** — write a review file that joins only the spans to be removed, plus their list
  - Why: what will be cut is checked before it is cut（なぜ要るか：削除予定の区間だけを繋いで確認できる）
  - Done when: the review file lasts the sum of the removed spans
  - Done (ADR-027): CONDENSE `output: removed` writes a review file of only the spans its rules cut, in order at speed 1, and its record lists each with its source range and its place in the file (`condense.review`, `removed`). `CondenseTests`: two retakes (0.5 s + 1.2 s) give a 1.7 s review file (within 0.1 s) beside the edit itself, from one request
- [ ] **18. Export to a target file size** — given a limit such as 25 MB, work out the video bitrate from the duration and the audio bitrate and write it in two passes through ffmpeg-skill; past the limit, work it out again and retry; when it cannot fit (resolution, lowest bitrate), fail with the reason
  - Why: deliveries with a size cap (mail, chat, upload limits) are met on the first try（なぜ要るか：サイズ上限のある納品を一度で満たす）
  - Done when: on two or three fixtures the output is at or under the limit and at least 90 % of it

## Boundaries

Still-image overlay also exists in motion-graphics `image_overlay`; ecosystem.json gives it to video-editing.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).

"""video-editing-skill: a deterministic, verifiable video editing Skill.

It turns a typed edit request (sources, operations, outputs) into an operation graph, compiles every
operation to a typed call of an ffmpeg-skill tool, runs it inside a workspace boundary and validates
the result. It holds no editing judgement: what to cut, which camera to use and why belong to the
caller (video-production-agent). See README.md.
"""

SKILL_ID = "video-editing"
PACKAGE_NAME = "video-editing-skill"
VERSION = "0.3.0"

# Two independent axes (docs/decisions.md ADR-007): VERSION is this package's own release
# version and can move on any release, including one that adds nothing a dependent needs to
# react to. CONTRACT_VERSION is the version of the *shape* the contract publishes (pinned
# blocks: operations, capabilities, schemas, execution guarantees, ...) and changes only when
# that shape changes in a breaking way - a dependent pins a range against CONTRACT_VERSION,
# never VERSION. Bumped to "3.0" for 0.3.0 (docs/decisions.md ADR-010): RESIZE gained a
# `height` alternative to `width`, and CROP / IMAGE_INSERT are new operation types - all three
# change the pinned `operations` block, breaking by this repo's own pinning convention.
CONTRACT_VERSION = "3.0"

REQUEST_SCHEMA = "video-editing/request@1"
RESPONSE_SCHEMA = "video-editing/response@1"
PLAN_SCHEMA = "video-editing/plan@1"
CONTRACT_SCHEMA = "video-editing/contract@1"
DOCTOR_SCHEMA = "video-editing/doctor@1"

__version__ = VERSION

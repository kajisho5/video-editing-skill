"""video-editing-skill: a deterministic, verifiable video editing Skill.

It turns a typed edit request (sources, operations, outputs) into an operation graph, compiles every
operation to a typed call of an ffmpeg-skill tool, runs it inside a workspace boundary and validates
the result. It holds no editing judgement: what to cut, which camera to use and why belong to the
caller (video-production-agent). See README.md.
"""

SKILL_ID = "video-editing"
PACKAGE_NAME = "video-editing-skill"
VERSION = "0.1.0"

REQUEST_SCHEMA = "video-editing/request@1"
RESPONSE_SCHEMA = "video-editing/response@1"
PLAN_SCHEMA = "video-editing/plan@1"
CONTRACT_SCHEMA = "video-editing/contract@1"
DOCTOR_SCHEMA = "video-editing/doctor@1"

__version__ = VERSION

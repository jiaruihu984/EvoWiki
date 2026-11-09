"""Role axis of the personal view (Stage I-B spec §1). Dependency-free so RenderPlan can use it."""

from enum import StrEnum


class Role(StrEnum):
    LEARN = "learn"
    SUPPORT = "support"
    REFERENCE = "reference"
    BRIDGE = "bridge"

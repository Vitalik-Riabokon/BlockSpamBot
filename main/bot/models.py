from dataclasses import dataclass
from enum import Enum


class ModerationStatus(str, Enum):
    SAFE_TEXT = "SAFE_TEXT"
    AD_BLOCKED = "AD_BLOCKED"
    AD_SUSPECT = "AD_SUSPECT"
    AD_PENDING_AUTH = "AD_PENDING_AUTH"
    AD_ALLOWED = "AD_ALLOWED"


@dataclass
class ModerationResult:
    status: ModerationStatus
    score: int
    reasons: list[str]
    ad_intent: bool


@dataclass
class GroupPolicy:
    group_id: int
    whitelist_user_ids: set[int]
    authorized_user_ids: set[int]
    hard_block_extra_keywords: set[str]

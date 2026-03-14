import time
from collections import defaultdict, deque

from . import config

StateKey = tuple[int, int]  # (group_id, user_id)

user_message_times: dict[StateKey, deque[float]] = defaultdict(lambda: deque())
user_fingerprints: dict[StateKey, deque[tuple[float, str]]] = defaultdict(lambda: deque())
user_suspect_times: dict[StateKey, deque[float]] = defaultdict(lambda: deque())
user_strikes: dict[StateKey, int] = defaultdict(int)
recent_messages: dict[StateKey, deque[tuple[float, int, str]]] = defaultdict(lambda: deque())
user_ad_fingerprints: dict[StateKey, deque[tuple[float, str]]] = defaultdict(lambda: deque())


def spam_pattern_score(group_id: int, author_id: int, normalized_text: str) -> int:
    score = 0
    now = time.time()
    key = (group_id, author_id)

    times = user_message_times[key]
    times.append(now)
    while times and now - times[0] > config.WINDOW_SECONDS:
        times.popleft()
    if len(times) >= config.FLOOD_COUNT:
        score += 20

    fingerprints = user_fingerprints[key]
    fingerprints.append((now, normalized_text))
    while fingerprints and now - fingerprints[0][0] > config.DUPLICATE_WINDOW_SECONDS:
        fingerprints.popleft()

    duplicates = sum(1 for _, item in fingerprints if item == normalized_text)
    if duplicates >= 2:
        score += 25

    return score


def user_reputation_score(group_id: int, author_id: int) -> int:
    strikes = user_strikes[(group_id, author_id)]
    if strikes == 0:
        return 0
    if strikes == 1:
        return 8
    if strikes == 2:
        return 15
    return 25


def add_strike(group_id: int, author_id: int) -> int:
    key = (group_id, author_id)
    user_strikes[key] += 1
    return user_strikes[key]


def mark_suspect(group_id: int, author_id: int) -> None:
    now = time.time()
    key = (group_id, author_id)
    queue = user_suspect_times[key]
    queue.append(now)
    while queue and now - queue[0] > config.SUSPECT_WINDOW_SECONDS:
        queue.popleft()


def should_escalate_suspect(group_id: int, author_id: int) -> bool:
    return len(user_suspect_times[(group_id, author_id)]) >= config.SUSPECT_ESCALATION_COUNT


def remember_recent_message(group_id: int, author_id: int, message_id: int, text: str) -> list[tuple[int, str]]:
    """
    Store recent message context for split-message analysis.
    Returns list of (message_id, text) in active window.
    """
    now = time.time()
    key = (group_id, author_id)
    queue = recent_messages[key]
    queue.append((now, message_id, text))

    while queue and now - queue[0][0] > config.SPLIT_WINDOW_SECONDS:
        queue.popleft()

    while len(queue) > config.SPLIT_MAX_MESSAGES:
        queue.popleft()

    return [(item[1], item[2]) for item in queue]


def ad_duplicate_count(group_id: int, author_id: int, normalized_text: str) -> int:
    """
    Count identical ad texts for this user in the configured duplicate window.
    Should be used only after message is recognized as ad.
    """
    now = time.time()
    key = (group_id, author_id)
    queue = user_ad_fingerprints[key]
    queue.append((now, normalized_text))

    while queue and now - queue[0][0] > config.AD_DUPLICATE_BLOCK_WINDOW_SECONDS:
        queue.popleft()

    return sum(1 for _, item in queue if item == normalized_text)

import time
from collections import defaultdict, deque

from . import config

StateKey = tuple[int, int]  # (group_id, user_id)

user_message_times: dict[StateKey, deque[float]] = defaultdict(lambda: deque())
user_fingerprints: dict[StateKey, deque[tuple[float, str]]] = defaultdict(lambda: deque())
user_suspect_times: dict[StateKey, deque[float]] = defaultdict(lambda: deque())
user_strikes: dict[StateKey, int] = defaultdict(int)


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

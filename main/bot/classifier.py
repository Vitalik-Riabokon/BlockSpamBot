"""Message classification helpers for ads, scam patterns and hard-block content."""

import re

from aiogram import types

from . import config, state
from .models import GroupPolicy, ModerationResult, ModerationStatus
from .rules import (
    AD_OFFER_KEYWORDS,
    CTA_KEYWORDS,
    HARD_ILLEGAL_KEYWORDS,
    SCAM_JOB_KEYWORDS,
    SUSPICIOUS_DOMAIN_WORDS,
    mention_regex,
    money_regex,
    phone_regex,
    simple_domain_regex,
    url_regex,
)


def normalize_obfuscated_domain(candidate: str) -> str:
    """Normalize intentionally obfuscated domain-like fragments into a comparable form."""
    value = candidate.lower()
    value = re.sub(r"\b(dot|точка|точкa|\[dot\]|\(dot\)|\{dot\})\b", ".", value)
    value = re.sub(r"[\s\[\]\(\)\{\}\|,;:+]+", ".", value)
    value = re.sub(r"[^a-z0-9\.\-]", "", value)
    value = re.sub(r"\.+", ".", value).strip(".")
    return value


def contains_suspicious_domain(text: str) -> bool:
    """Return True when text contains a direct or obfuscated suspicious domain signal."""
    if url_regex.search(text):
        return True

    if simple_domain_regex.search(text):
        return True

    candidates = re.findall(r"[\w\-\[\]\(\)\{\}\s]{3,50}", text)
    for candidate in candidates:
        normalized = normalize_obfuscated_domain(candidate)
        if normalized and simple_domain_regex.search(normalized):
            return True
        for word in SUSPICIOUS_DOMAIN_WORDS:
            if word in normalized:
                return True
    return False


def normalize_text_for_match(text: str) -> str:
    """Normalize text for keyword matching across minor locale and whitespace variations."""
    cleaned = text.lower()
    cleaned = cleaned.replace("ё", "е").replace("і", "i")
    cleaned = re.sub(r"[\u200b-\u200f\u2060]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def get_text(message: types.Message) -> str:
    """Extract plain moderation text from message body and caption."""
    text = (message.text or "") + " " + (getattr(message, "caption", "") or "")
    return text.strip()


def has_any_keyword(text_lower: str, keywords: list[str] | set[str]) -> bool:
    """Check whether normalized text contains any normalized keyword."""
    normalized_text = normalize_text_for_match(text_lower)
    for keyword in keywords:
        normalized_keyword = normalize_text_for_match(str(keyword))
        if normalized_keyword and normalized_keyword in normalized_text:
            return True
    return False


def is_authorized_ad(author: types.User, policy: GroupPolicy) -> bool:
    """Return whether the author is currently legalised for this group."""
    return author.id in policy.authorized_user_ids


def has_contact_signal(message: types.Message, text: str) -> bool:
    """Detect contact or delivery signals that make an ad actionable."""
    if url_regex.search(text) or simple_domain_regex.search(text):
        return True
    if contains_suspicious_domain(text):
        return True
    if mention_regex.search(text):
        return True
    if phone_regex.search(text):
        return True
    if message.entities:
        for entity in message.entities:
            if entity.type in ("url", "text_link", "email", "phone_number"):
                return True
    return False


def ad_intent(message: types.Message, text: str) -> tuple[bool, list[str]]:
    """Estimate whether the message has enough offer/contact/CTA signals to be treated as an ad."""
    text_lower = normalize_text_for_match(text)
    reasons = []
    signals = 0

    if has_any_keyword(text_lower, AD_OFFER_KEYWORDS):
        signals += 1
        reasons.append("offer_keyword")
    if has_any_keyword(text_lower, CTA_KEYWORDS):
        signals += 1
        reasons.append("cta_keyword")
    if has_contact_signal(message, text_lower):
        signals += 1
        reasons.append("contact_or_link")

    return signals >= 2, reasons


def hard_illegal_detected(text_lower: str, policy: GroupPolicy) -> bool:
    """Detect hard-block illegal content from built-in or group-specific triggers."""
    return has_any_keyword(text_lower, HARD_ILLEGAL_KEYWORDS) or has_any_keyword(
        text_lower, policy.hard_block_extra_keywords
    )


def classify_message(message: types.Message, policy: GroupPolicy) -> ModerationResult:
    """Classify a Telegram message into safe, pending, suspect or blocked ad status."""
    text = get_text(message)
    if not text:
        return ModerationResult(ModerationStatus.SAFE_TEXT, 0, ["no_text"], False)

    author = message.from_user
    if not author:
        return ModerationResult(ModerationStatus.SAFE_TEXT, 0, ["no_author"], False)

    text_lower = normalize_text_for_match(text)
    is_ad, ad_reasons = ad_intent(message, text_lower)
    reasons = list(ad_reasons)
    score = 0
    has_contact = has_contact_signal(message, text_lower)
    has_cta = has_any_keyword(text_lower, CTA_KEYWORDS)
    has_money = bool(money_regex.search(text_lower))
    has_susp_domain = contains_suspicious_domain(text_lower)
    has_susp_domain_word = has_any_keyword(text_lower, SUSPICIOUS_DOMAIN_WORDS)
    has_scam_job = has_any_keyword(text_lower, SCAM_JOB_KEYWORDS)
    has_job_core = has_any_keyword(
        text_lower,
        {
            "робота з дому",
            "удаленка",
            "удалёнка",
            "без опыта",
            "без досвіду",
            "дохід",
            "доход",
            "2 часа",
            "2 години",
        },
    )

    # Hard-illegal content is blocked when it also carries delivery/contact/CTA signal.
    if hard_illegal_detected(text_lower, policy):
        if has_contact or has_cta or has_money or has_susp_domain:
            reasons.append("hard_illegal")
            return ModerationResult(ModerationStatus.AD_BLOCKED, 100, reasons, True)
    if has_susp_domain and has_susp_domain_word:
        reasons.append("suspicious_domain_word")
        return ModerationResult(ModerationStatus.AD_BLOCKED, 100, reasons, True)

    if not is_ad:
        return ModerationResult(ModerationStatus.SAFE_TEXT, 0, ["not_ad"], False)

    if has_scam_job and has_job_core and has_contact and (has_cta or has_money):
        reasons.append("scam_job_hard")
        return ModerationResult(ModerationStatus.AD_BLOCKED, 100, reasons, True)

    if has_scam_job:
        score += 35
        reasons.append("scam_job_pattern")

    if contains_suspicious_domain(text_lower):
        score += 25
        reasons.append("suspicious_link")

    if re.search(r"[^\w\s]{3,}", text_lower) and has_any_keyword(text_lower, SUSPICIOUS_DOMAIN_WORDS):
        score += 20
        reasons.append("obfuscation_like")

    if money_regex.search(text_lower):
        score += 8
        reasons.append("money_claim")

    pattern_score = state.spam_pattern_score(policy.group_id, author.id, text_lower)
    if pattern_score:
        score += pattern_score
        reasons.append("spam_pattern")

    reputation = state.user_reputation_score(policy.group_id, author.id)
    if reputation:
        score += reputation
        reasons.append("user_reputation")

    if score >= config.BLOCK_SCORE_THRESHOLD:
        reasons.append("score_block")
        return ModerationResult(ModerationStatus.AD_BLOCKED, score, reasons, True)

    if score >= config.SUSPECT_SCORE_THRESHOLD:
        reasons.append("score_suspect")
        return ModerationResult(ModerationStatus.AD_SUSPECT, score, reasons, True)

    if is_authorized_ad(author, policy):
        reasons.append("authorized")
        return ModerationResult(ModerationStatus.AD_ALLOWED, score, reasons, True)

    reasons.append("pending_authorization")
    return ModerationResult(ModerationStatus.AD_PENDING_AUTH, score, reasons, True)

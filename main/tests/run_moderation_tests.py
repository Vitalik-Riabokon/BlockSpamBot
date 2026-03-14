from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config, state  # noqa: E402
from bot.classifier import (  # noqa: E402
    classify_message,
    contains_suspicious_domain,
    hard_illegal_detected,
    has_any_keyword,
    normalize_text_for_match,
)
from bot.models import GroupPolicy, ModerationStatus  # noqa: E402
from bot.rules import CTA_KEYWORDS, SCAM_JOB_KEYWORDS, mention_regex, phone_regex  # noqa: E402


@dataclass
class FakeUser:
    id: int
    full_name: str = "User"


@dataclass
class FakeChat:
    id: int
    type: str = "supergroup"
    title: str = "Test Group"


@dataclass
class FakeEntity:
    type: str


@dataclass
class FakeMessage:
    text: str
    from_user: FakeUser
    chat: FakeChat
    caption: str = ""
    entities: List[FakeEntity] | None = None


@dataclass
class Case:
    case_id: str
    group: str
    title: str
    text: str
    expected_decision: str
    user_id: int = 1001
    group_id: int = -100001
    whitelist: set[int] | None = None
    authorized: set[int] | None = None
    hardwords: set[str] | None = None
    reset_state: bool = True
    note: str = ""


@dataclass
class ResultRow:
    case_id: str
    group: str
    title: str
    expected: str
    actual: str
    status: str
    reasons: str
    note: str


_MSG_SEQ = 0


def reset_runtime_state() -> None:
    state.user_message_times.clear()
    state.user_fingerprints.clear()
    state.user_suspect_times.clear()
    state.user_strikes.clear()
    state.recent_messages.clear()
    state.user_ad_fingerprints.clear()


def split_chain_block(policy: GroupPolicy, recent_items: list[tuple[int, str]]) -> bool:
    if len(recent_items) < config.SPLIT_MAX_MESSAGES:
        return False
    combined = " ".join([text for _, text in recent_items if text]).strip()
    if not combined:
        return False
    normalized = normalize_text_for_match(combined)
    has_contact = contains_suspicious_domain(normalized) or bool(mention_regex.search(normalized) or phone_regex.search(normalized))
    has_cta = has_any_keyword(normalized, CTA_KEYWORDS)
    has_scam_job = has_any_keyword(normalized, SCAM_JOB_KEYWORDS)
    if hard_illegal_detected(normalized, policy) and has_contact:
        return True
    if has_scam_job and has_cta and has_contact:
        return True
    return False


def evaluate(case: Case) -> tuple[str, str]:
    global _MSG_SEQ
    if case.reset_state:
        reset_runtime_state()

    whitelist = case.whitelist or set()
    authorized = (case.authorized or set()) | whitelist
    hardwords = case.hardwords or set()

    policy = GroupPolicy(
        group_id=case.group_id,
        whitelist_user_ids=whitelist,
        authorized_user_ids=authorized,
        hard_block_extra_keywords=hardwords,
    )

    msg = FakeMessage(
        text=case.text,
        from_user=FakeUser(id=case.user_id, full_name=f"User {case.user_id}"),
        chat=FakeChat(id=case.group_id),
    )
    _MSG_SEQ += 1
    recent_items = state.remember_recent_message(case.group_id, case.user_id, _MSG_SEQ, case.text)

    result = classify_message(msg, policy)
    split_block = split_chain_block(policy, recent_items)
    if result.status == ModerationStatus.SAFE_TEXT and split_block:
        decision = "DELETE_BLOCK"
        reasons = "status=AD_BLOCKED; reasons=split_chain_block; score=75"
        return decision, reasons
    if result.status != ModerationStatus.AD_BLOCKED and split_block:
        decision = "DELETE_BLOCK"
        reasons = f"status=AD_BLOCKED; reasons={','.join(result.reasons)},split_chain_block; score={max(result.score, config.BLOCK_SCORE_THRESHOLD)}"
        return decision, reasons

    if result.status != ModerationStatus.SAFE_TEXT:
        dup_count = state.ad_duplicate_count(case.group_id, case.user_id, normalize_text_for_match(case.text))
        if dup_count >= config.AD_DUPLICATE_BLOCK_COUNT and result.status != ModerationStatus.AD_BLOCKED:
            decision = "DELETE_BLOCK"
            reasons = f"status=AD_BLOCKED; reasons={','.join(result.reasons)},ad_duplicate_block; score={max(result.score, config.BLOCK_SCORE_THRESHOLD)}"
            return decision, reasons

    in_whitelist = case.user_id in whitelist
    hard_block_hit = "hard_illegal" in result.reasons
    duplicate_block_hit = "ad_duplicate_block" in result.reasons

    if result.status == ModerationStatus.SAFE_TEXT:
        decision = "ALLOW"
    elif result.status == ModerationStatus.AD_BLOCKED:
        if in_whitelist and not hard_block_hit and not split_block and not duplicate_block_hit:
            decision = "ALLOW_WHITELIST_NO_ALERT"
        else:
            decision = "DELETE_BLOCK" if not in_whitelist else "DELETE_HARD_WHITELIST_ALERT"
    elif result.status == ModerationStatus.AD_SUSPECT:
        state.mark_suspect(case.group_id, case.user_id)
        if state.should_escalate_suspect(case.group_id, case.user_id):
            decision = "DELETE_ESCALATED"
        else:
            decision = "REVIEW_ALERT"
    elif result.status == ModerationStatus.AD_PENDING_AUTH:
        decision = "REVIEW_ALERT"
    elif result.status == ModerationStatus.AD_ALLOWED:
        decision = "ALLOW_WHITELIST_NO_ALERT" if in_whitelist else "ALLOW_AUTHORIZED"
    else:
        decision = "UNKNOWN"

    reasons = f"status={result.status.value}; reasons={','.join(result.reasons)}; score={result.score}"
    return decision, reasons


def run_cases(cases: list[Case]) -> list[ResultRow]:
    rows: list[ResultRow] = []
    for case in cases:
        actual, reasons = evaluate(case)
        status = "PASS" if actual == case.expected_decision else "FAIL"
        rows.append(
            ResultRow(
                case_id=case.case_id,
                group=case.group,
                title=case.title,
                expected=case.expected_decision,
                actual=actual,
                status=status,
                reasons=reasons,
                note=case.note,
            )
        )
    return rows


def build_cases() -> list[Case]:
    wl_user = 7777
    auth_user = 5555

    cases: list[Case] = [
        # A. Simple conversations (should not trigger ad logic)
        Case("A01", "A. Simple Chat", "Family logistics", "Хто може забрати дітей о 18:00?", "ALLOW"),
        Case("A02", "A. Simple Chat", "Asking for doctor", "Порадьте дитячого лікаря в Байройті, будь ласка", "ALLOW"),
        Case("A03", "A. Simple Chat", "Casual DM phrase only", "го в лс", "ALLOW"),
        Case("A04", "A. Simple Chat", "Community event info", "Завтра зустріч о 17:00 в центрі", "ALLOW"),
        Case("A05", "A. Simple Chat", "German chat", "Wer geht heute zum Kurs?", "ALLOW"),
        Case("A06", "A. Simple Chat", "Question with phone-like number", "Мій новий номер 0157 123 4567", "ALLOW"),
        Case("A07", "A. Simple Chat", "Link without ad intent", "Подивіться новини: https://dw.com", "ALLOW"),
        Case("A08", "A. Simple Chat", "Political slogan without ad intent", "слава росії", "ALLOW", note="Gap vs strict policy: currently blocked only in ad context"),

        # B. Clean ads / sanctioned status checks
        Case("B01", "B. Non-Critical Ads", "Educational ad with details", "Інфо-зустріч про Ausbildung. Реєстрація на site.de, пишіть для деталей", "REVIEW_ALERT"),
        Case("B02", "B. Non-Critical Ads", "Simple service ad", "Послуга перекладу документів, пишіть в лс @translator", "REVIEW_ALERT"),
        Case("B03", "B. Non-Critical Ads", "Authorized advertiser", "Послуга перекладу документів, пишіть в лс @translator", "ALLOW_AUTHORIZED", user_id=auth_user, authorized={auth_user}),
        Case("B04", "B. Non-Critical Ads", "Whitelist trusted advertiser no alerts", "Освітній курс, пишіть в лс @school", "ALLOW_WHITELIST_NO_ALERT", user_id=wl_user, whitelist={wl_user}),

        # C. Hard blocked content
        Case("C01", "C. Hard Block", "Casino + CTA + contact", "Казино бонус, пиши в лс @topwin", "DELETE_BLOCK"),
        Case("C02", "C. Hard Block", "Betting + URL", "Ставки на спорт, переходь https://fastbet.top", "DELETE_BLOCK"),
        Case("C03", "C. Hard Block", "Obfuscated casino domain", "Занос, пиши в лс bonusspin [dot] xo [dot] je", "DELETE_BLOCK"),
        Case("C04", "C. Hard Block", "Pro-russian slogan in ad context", "Слава росії, переходь на канал @zzpromo", "DELETE_BLOCK"),
        Case("C05", "C. Hard Block", "Custom hardword", "Пиши в лс @deal, mega-scam", "DELETE_BLOCK", hardwords={"mega-scam"}),
        Case("C06", "C. Hard Block", "Whitelist user with hard block", "Казино бонус, пиши в лс @vip", "DELETE_HARD_WHITELIST_ALERT", user_id=wl_user, whitelist={wl_user}),

        # D. Suspect / gray job ads
        Case("D01", "D. Gray Ads", "Remote job template", "Удаленка, 2 часа в день, без опыта, пиши в лс @jobfast", "DELETE_BLOCK"),
        Case("D02", "D. Gray Ads", "Income claim", "Робота з дому, дохід 100€ в день, пишіть в директ @easywork", "DELETE_BLOCK"),
        Case("D03", "D. Gray Ads", "No explicit contact still ad-intent", "Курс навчання, знижка сьогодні, реєструйся на site.de", "REVIEW_ALERT"),

        # E. Flood / duplicate logic
        Case("E01", "E. Spam Pattern", "Base suspicious message #1", "Робота з дому, без досвіду, пиши в лс @xjob", "DELETE_BLOCK", user_id=4001, reset_state=True),
        Case("E02", "E. Spam Pattern", "Duplicate suspicious message #2", "Робота з дому, без досвіду, пиши в лс @xjob", "DELETE_BLOCK", user_id=4001, reset_state=False),
        Case("E03", "E. Spam Pattern", "Flood msg #1", "Послуга, пиши в лс @service", "REVIEW_ALERT", user_id=4010, reset_state=True),
        Case("E04", "E. Spam Pattern", "Flood msg #2", "Послуга, пиши в лс @service", "REVIEW_ALERT", user_id=4010, reset_state=False),
        Case("E05", "E. Spam Pattern", "Flood msg #3", "Послуга, пиши в лс @service", "DELETE_BLOCK", user_id=4010, reset_state=False),
        Case("E06", "E. Spam Pattern", "Flood msg #4", "Послуга, пиши в лс @service", "DELETE_BLOCK", user_id=4010, reset_state=False),

        # F. Split-message ad tactics
        Case("F01", "F. Split Ads", "Part 1 (offer only)", "Робота онлайн 2 години на день", "ALLOW", user_id=5001, reset_state=True),
        Case("F02", "F. Split Ads", "Part 2 (CTA+contact)", "Пиши в лс @workhelp", "REVIEW_ALERT", user_id=5001, reset_state=False),
        Case("F03", "F. Split Ads", "Part 3 (money claim)", "Дохід 80€ в день", "ALLOW", user_id=5001, reset_state=False),
        Case("F04", "F. Split Ads", "Part 4 (url only)", "https://job-fast.site", "DELETE_BLOCK", user_id=5001, reset_state=False, note="Split chain should be blocked"),

        # G. Extra false-positive guard checks
        Case("G01", "G. False Positive Guard", "School parent message with emojis", "Матусі й татусі, покажіть дітям відео перед сном ❤️", "ALLOW"),
        Case("G02", "G. False Positive Guard", "Local announcement no CTA", "Зустріч українців у суботу о 15:00 у парку", "ALLOW"),
        Case("G03", "G. False Positive Guard", "Question with @mention", "Хтось знає @ivan?", "ALLOW"),
        Case("G04", "G. False Positive Guard", "Phone and no ad", "Мій номер для сусідів +49 123 123456", "ALLOW"),
        Case("G05", "G. False Positive Guard", "Legit bilingual info post", "Інфо-зустріч для українців у Німеччині, 09.12 о 17:00, www.fachwirt-akademie.de", "REVIEW_ALERT"),

        # H. Real group samples (based on info.txt)
        Case(
            "H01",
            "H. Real Group Samples",
            "Ride UA->DE service announcement",
            "Їдемо в Україну, бронювання без передоплати, ліцензія, +380676386898",
            "ALLOW",
            note="Current model treats this as non-ad due missing explicit CTA signal",
        ),
        Case("H02", "H. Real Group Samples", "Question about film language", "Мавка на українській мові буде?", "ALLOW"),
        Case("H03", "H. Real Group Samples", "Film info reply", "Мавка фільм в оригіналі - українською з німецькими субтитрами", "ALLOW"),
        Case(
            "H04",
            "H. Real Group Samples",
            "RU ride message with services",
            "Здравствуйте 6 числа еду в Украину могу взять передачи, купить что-то в аптеке",
            "ALLOW",
            note="Policy gap: ads should be only Ukrainian, language control not implemented yet",
        ),
        Case(
            "H05",
            "H. Real Group Samples",
            "Tours + contacts + links",
            "Пропоную поїздки та тури. Резервуємо місця вже. https://t.me/priemnipoizdki +4917624354644",
            "REVIEW_ALERT",
        ),
        Case("H06", "H. Real Group Samples", "Sell laptop", "Ноутбук Acer, ціна 90 євро", "ALLOW"),
        Case("H07", "H. Real Group Samples", "Sell TV", "Продам Smart TV, ціна 120 євро", "ALLOW"),
        Case(
            "H08",
            "H. Real Group Samples",
            "Pizza place promo with booking phone",
            "Затишна атмосфера, дров'яна піч, бронюйте столики 0921 64199",
            "ALLOW",
            note="Likely local business ad; no explicit ad_intent in current rules",
        ),
        Case(
            "H09",
            "H. Real Group Samples",
            "Parcel service recurring",
            "Регулярно кожної суботи збір та видача посилок, Bayreuth, +491639454555, https://t.me/buzinskiy",
            "REVIEW_ALERT",
        ),
        Case(
            "H10",
            "H. Real Group Samples",
            "Cinema event with URL",
            "У кінотеатрі Cineplex відбудеться показ українського фільму, https://www.cineplex.de/bayreuth",
            "ALLOW",
            note="Community info; model currently mostly treats as non-ad",
        ),
        Case("H11", "H. Real Group Samples", "Looking for haircut", "Потрібна професійна стрижка, порадьте когось", "ALLOW"),
        Case("H12", "H. Real Group Samples", "Request for tech help", "В ДІЯ активована підпис, хто може допомогти налаштувати?", "ALLOW"),
        Case("H13", "H. Real Group Samples", "Flea market question", "Хто знає коли барахолка в Байройті?", "ALLOW"),
        Case("H14", "H. Real Group Samples", "Waste sorting question", "Куди викидати пластикові відра з-під фарби?", "ALLOW"),
        Case(
            "H15",
            "H. Real Group Samples",
            "Child camp registration reminder",
            "Реєстрація на Talent Campus, напишіть в особисті до суботи, місця обмежені",
            "ALLOW",
            note="Community/NGO style info post, should not be blocked",
        ),

        # I. Split-message tactics (aggressive)
        Case("I01", "I. Split Ad Tactics", "Split part 1", "Удаленка 2 часа в день", "ALLOW", user_id=9001, reset_state=True),
        Case("I02", "I. Split Ad Tactics", "Split part 2", "Доход 80 евро", "ALLOW", user_id=9001, reset_state=False),
        Case("I03", "I. Split Ad Tactics", "Split part 3", "Без опыта, обучаем", "ALLOW", user_id=9001, reset_state=False),
        Case("I04", "I. Split Ad Tactics", "Split part 4", "Пиши в директ @workgo", "DELETE_BLOCK", user_id=9001, reset_state=False),
        Case("I05", "I. Split Ad Tactics", "Split part 5", "https://jobbonus.site", "DELETE_BLOCK", user_id=9001, reset_state=False),
        Case("I06", "I. Split Ad Tactics", "Split casino part 1", "Занос на 140к баксов", "DELETE_BLOCK", user_id=9002, reset_state=True),
        Case("I07", "I. Split Ad Tactics", "Split casino part 2", "играю тут bonusspin.xo.je", "DELETE_BLOCK", user_id=9002, reset_state=False),
    ]

    return cases


def write_report(rows: list[ResultRow], target: Path) -> None:
    total = len(rows)
    passed = sum(1 for row in rows if row.status == "PASS")
    failed = total - passed

    groups: dict[str, list[ResultRow]] = {}
    for row in rows:
        groups.setdefault(row.group, []).append(row)

    lines: list[str] = []
    lines.append("# Moderation Test Report")
    lines.append("")
    lines.append(f"- Total cases: **{total}**")
    lines.append(f"- Passed: **{passed}**")
    lines.append(f"- Failed: **{failed}**")
    lines.append(f"- Thresholds: suspect={config.SUSPECT_SCORE_THRESHOLD}, block={config.BLOCK_SCORE_THRESHOLD}")
    lines.append("")

    for group_name, group_rows in groups.items():
        lines.append(f"## {group_name}")
        lines.append("")
        lines.append("| ID | Case | Expected | Actual | Status | Notes |")
        lines.append("|---|---|---|---|---|---|")
        for row in group_rows:
            note = row.note or row.reasons
            note = note.replace("|", "\\|")
            lines.append(
                f"| {row.case_id} | {row.title} | {row.expected} | {row.actual} | {row.status} | {note} |"
            )
        lines.append("")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    cases = build_cases()
    rows = run_cases(cases)
    report_path = Path("main/tests/moderation_test_report.md")
    write_report(rows, report_path)

    total = len(rows)
    failed = sum(1 for row in rows if row.status == "FAIL")
    print(f"Report written: {report_path}")
    print(f"Total={total} Failed={failed}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

import re

HARD_ILLEGAL_KEYWORDS = [
    "казино", "казик", "казiно", "ставки", "букмек", "букмекер", "бет", "bet", "slots",
    "слот", "азарт", "гембл", "гемблінг", "занос", "вывожу", "вывести",
    "слава россии", "слава росії", "za родину", "русский мир", "руский мир",
]

SCAM_JOB_KEYWORDS = [
    "удаленка", "удалёнка", "2 часа", "2 год", "без опыта", "обучаем", "пиши в лс", "в директ",
    "легкий доход", "доход в день", "від 50", "від 100", "робота з дому",
]

AD_OFFER_KEYWORDS = [
    "ваканс", "робота", "послуга", "услуга", "курс", "навчання", "обучение",
    "запрошуємо", "приглашаем", "купити", "продам", "продажа", "скидка",
]

CTA_KEYWORDS = [
    "пиши", "пишіть", "в лс", "в директ", "details", "подробности", "звертайтесь",
    "реєструйся", "register", "переходь", "переходите", "запис",
]

SUSPICIOUS_DOMAIN_WORDS = [
    "casino", "bet", "bets", "gamble", "gambling", "777", "bonus", "spin", "bukmeker"
]

url_regex = re.compile(r"https?://\S+", flags=re.IGNORECASE)
simple_domain_regex = re.compile(
    r"\b(?:[a-z0-9\-]{1,63}\.)+(?:com|de|ru|net|org|io|xyz|top|online|site|bet|casino|je|xo|tk|ml|ua|biz|shop)\b",
    flags=re.IGNORECASE,
)
mention_regex = re.compile(r"@\w{4,}")
phone_regex = re.compile(r"(\+?\d[\d\-\s()]{5,}\d)")
money_regex = re.compile(
    r"(\d{1,3}(?:[ ,.\u00A0]\d{3})*(?:\$|€|eur|usd|бакс|баксів|баксов|к\b|k\b)?)",
    flags=re.IGNORECASE,
)


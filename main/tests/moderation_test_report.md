# Moderation Test Report

- Total cases: **58**
- Passed: **58**
- Failed: **0**
- Thresholds: suspect=45, block=75

## A. Simple Chat

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| A01 | Family logistics | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A02 | Asking for doctor | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A03 | Casual DM phrase only | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A04 | Community event info | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A05 | German chat | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A06 | Question with phone-like number | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A07 | Link without ad intent | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| A08 | Political slogan without ad intent | ALLOW | ALLOW | PASS | Gap vs strict policy: currently blocked only in ad context |

## B. Non-Critical Ads

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| B01 | Educational ad with details | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,cta_keyword,contact_or_link,suspicious_link,pending_authorization; score=25 |
| B02 | Simple service ad | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,cta_keyword,contact_or_link,pending_authorization; score=0 |
| B03 | Authorized advertiser | ALLOW_AUTHORIZED | ALLOW_AUTHORIZED | PASS | status=AD_ALLOWED; reasons=offer_keyword,cta_keyword,contact_or_link,authorized; score=0 |
| B04 | Whitelist trusted advertiser no alerts | ALLOW_WHITELIST_NO_ALERT | ALLOW_WHITELIST_NO_ALERT | PASS | status=AD_ALLOWED; reasons=offer_keyword,cta_keyword,contact_or_link,authorized; score=0 |

## C. Hard Block

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| C01 | Casino + CTA + contact | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,hard_illegal; score=100 |
| C02 | Betting + URL | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,hard_illegal; score=100 |
| C03 | Obfuscated casino domain | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,hard_illegal; score=100 |
| C04 | Pro-russian slogan in ad context | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,hard_illegal; score=100 |
| C05 | Custom hardword | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,hard_illegal; score=100 |
| C06 | Whitelist user with hard block | DELETE_HARD_WHITELIST_ALERT | DELETE_HARD_WHITELIST_ALERT | PASS | status=AD_BLOCKED; reasons=cta_keyword,hard_illegal; score=100 |

## D. Gray Ads

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| D01 | Remote job template | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,scam_job_hard; score=100 |
| D02 | Income claim | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_hard; score=100 |
| D03 | No explicit contact still ad-intent | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,cta_keyword,contact_or_link,suspicious_link,pending_authorization; score=25 |

## E. Spam Pattern

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| E01 | Base suspicious message #1 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_hard; score=100 |
| E02 | Duplicate suspicious message #2 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_hard; score=100 |
| E03 | Flood msg #1 | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_pattern,pending_authorization; score=35 |
| E04 | Flood msg #2 | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_SUSPECT; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_pattern,spam_pattern,score_suspect; score=60 |
| E05 | Flood msg #3 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_pattern,spam_pattern,score_suspect,ad_duplicate_block; score=75 |
| E06 | Flood msg #4 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=offer_keyword,cta_keyword,contact_or_link,scam_job_pattern,spam_pattern,score_block; score=80 |

## F. Split Ads

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| F01 | Part 1 (offer only) | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| F02 | Part 2 (CTA+contact) | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=cta_keyword,contact_or_link,scam_job_pattern,pending_authorization; score=35 |
| F03 | Part 3 (money claim) | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| F04 | Part 4 (url only) | DELETE_BLOCK | DELETE_BLOCK | PASS | Split chain should be blocked |

## G. False Positive Guard

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| G01 | School parent message with emojis | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| G02 | Local announcement no CTA | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| G03 | Question with @mention | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| G04 | Phone and no ad | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| G05 | Legit bilingual info post | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,contact_or_link,suspicious_link,money_claim,pending_authorization; score=33 |

## H. Real Group Samples

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| H01 | Ride UA->DE service announcement | ALLOW | ALLOW | PASS | Current model treats this as non-ad due missing explicit CTA signal |
| H02 | Question about film language | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H03 | Film info reply | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H04 | RU ride message with services | ALLOW | ALLOW | PASS | Policy gap: ads should be only Ukrainian, language control not implemented yet |
| H05 | Tours + contacts + links | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,contact_or_link,suspicious_link,money_claim,pending_authorization; score=33 |
| H06 | Sell laptop | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H07 | Sell TV | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H08 | Pizza place promo with booking phone | ALLOW | ALLOW | PASS | Likely local business ad; no explicit ad_intent in current rules |
| H09 | Parcel service recurring | REVIEW_ALERT | REVIEW_ALERT | PASS | status=AD_PENDING_AUTH; reasons=offer_keyword,contact_or_link,suspicious_link,money_claim,pending_authorization; score=33 |
| H10 | Cinema event with URL | ALLOW | ALLOW | PASS | Community info; model currently mostly treats as non-ad |
| H11 | Looking for haircut | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H12 | Request for tech help | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H13 | Flea market question | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H14 | Waste sorting question | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| H15 | Child camp registration reminder | ALLOW | ALLOW | PASS | Community/NGO style info post, should not be blocked |

## I. Split Ad Tactics

| ID | Case | Expected | Actual | Status | Notes |
|---|---|---|---|---|---|
| I01 | Split part 1 | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| I02 | Split part 2 | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| I03 | Split part 3 | ALLOW | ALLOW | PASS | status=SAFE_TEXT; reasons=not_ad; score=0 |
| I04 | Split part 4 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=cta_keyword,contact_or_link,scam_job_pattern,pending_authorization,split_chain_block; score=75 |
| I05 | Split part 5 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=contact_or_link,suspicious_domain_word; score=100 |
| I06 | Split casino part 1 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=hard_illegal; score=100 |
| I07 | Split casino part 2 | DELETE_BLOCK | DELETE_BLOCK | PASS | status=AD_BLOCKED; reasons=contact_or_link,suspicious_domain_word; score=100 |

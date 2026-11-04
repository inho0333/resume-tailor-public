"""Extracts words/phrases that recur in the raw job description, to feed into the resume
rewrite prompt.

Why: many companies use an ATS (automated resume screening) for the first pass, and a good
number of those weight "exact string match" against the job description keywords — often with
no synonym handling at all. jd_profile["keywords"], which jd_extractor gets by asking the LLM,
is the LLM's subjective judgement of what looks important; this module is purely
frequency-based, so it does not miss "the words this company / this JD actually repeats".
The two are different in kind, so they complement rather than replace each other — this one
goes into the tailor_engine prompt as its own section.

No dependencies (just regex + collections) — it does not pull in anything heavy like
trafilatura or anthropic.
"""

import re
from collections import Counter

# Ordinary English stopwords + JD/job-posting boilerplate (words that recur in nearly every
# JD regardless of the company). The second group has to be generous so that only the words
# specific to the candidate's skills/domain survive.
STOPWORDS = frozenset("""
a an the and or but if of to in on at by for with from as is are was were be been being
this that these those it its their our your you we they he she i
will would can could should may might must shall
have has had do does did not no nor so than then thus
job description role position candidate candidates applicant applicants employee employees
employment company companies team teams work working works worked opportunity opportunities
experience experiences skill skills ability abilities responsible responsibility responsibilities
including include includes required requirements requirement preferred qualification qualifications
minimum currently pursuing degree bachelor bachelors master masters mba phd program programs
summer intern internship interns weeks week commit able access all any both each few more most
other some such only own same also just about above below up down out off over under again
further once here there when where why how all any both each other some such
per hour hourly rate benefit benefits insurance holiday holidays paid sick time
apply applying application applications submit submitting accept agree policy privacy legal
right rights reserve reserves modify change notice without regard county state federal law laws
records record criminal arrest conviction unincorporated fair chance ordinance employer employers
new york los angeles san francisco seattle washington california
""".split())

# Phrases of 1-3 words are extracted, but a phrase is dropped if any of its words is a stopword.
_MAX_NGRAM = 3
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{1,}")

# Collapses domain terms whose spelling commonly varies — without this, "e-commerce" and
# "eCommerce" are counted as separate words and both scatter below the min_count threshold.
# Indiscriminate stemming/normalisation risks wrongly merging unrelated words, so only the
# cases actually run into in practice are registered explicitly.
_NORMALIZE = {
    "ecommerce": "e-commerce",
    "e-commerce": "e-commerce",
}


def _tokenize(text):
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    return [_NORMALIZE.get(t, t) for t in tokens]


def extract_keywords(text, top_n=15, min_count=2):
    """Returns: [(term, count), ...] — count descending; on a tie, phrases beat single words.

    Why min_count=2 is the default: the requirement itself — "words repeated several times" —
    acts as the filter. Legal boilerplate sentences are almost never repeated within a JD, so
    this threshold alone naturally screens out most of them (the stopword list does not have
    to be perfect).
    """
    tokens = _tokenize(text)
    n = len(tokens)

    counts = Counter()
    for size in range(1, _MAX_NGRAM + 1):
        for i in range(n - size + 1):
            gram_tokens = tokens[i : i + size]
            if any(t in STOPWORDS for t in gram_tokens):
                continue
            if size == 1 and len(gram_tokens[0]) <= 2:
                continue
            counts[" ".join(gram_tokens)] += 1

    candidates = [(term, c) for term, c in counts.items() if c >= min_count]
    # Frequency descending comes first — even a short word should rank above a phrase if it
    # really was repeated more often ("e-commerce" 5 times vs "e-commerce business" 2 times
    # means the former is the stronger signal). Longer phrases win only on a tie (at equal
    # frequency, the more specific wording is more useful in the prompt).
    candidates.sort(key=lambda tc: (tc[1], len(tc[0].split())), reverse=True)

    # Deliberately no logic that drops a term because of a subset relationship with another —
    # "e-commerce" and "e-commerce business" both surviving does no harm in the prompt (the
    # LLM understands the overlap on its own), while such logic has the worse side effect of
    # erasing the real signal (the higher-frequency shorter word). Confirmed while testing
    # against real job descriptions.
    return candidates[:top_n]


def format_for_prompt(keyword_counts):
    if not keyword_counts:
        return "(no repeated keywords extracted)"
    return "\n".join(f"- {term} ({count}x)" for term, count in keyword_counts)

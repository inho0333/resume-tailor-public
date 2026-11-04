"""Store for pending "confirm, then generate" requests — a local JSON file written
atomically. The auto-tailor process (`run.py cmd_tailor`) calls register() and exits;
the always-on listener process (`bot_listener.py`) calls get() when a Telegram button
is pressed and carries on through confirm_handler.py — the two processes talk to each
other through this file.

With no answer, a request expires after `config.CONFIRM_TTL_DAYS`. It used to wait
forever, which was changed on a user decision: unpressed requests piled up to 8, and
every record carried the whole JD body, so the file just grew. `prune_expired()` makes
the expiry call, and the call site is the polling loop of the always-on bot_listener
(so as not to add another cron job). The pressing side is covered too: `get()` refuses
to return an expired record, so pressing a button before pruning has run still lands in
confirm_handler's "expired / duplicate click" path."""

import datetime
import json
import os
import re
import tempfile
import uuid

import config

# `uuid` rather than the standard library `secrets` module: a short_id only has to be
# collision-free, not unguessable, and a local `secrets.py` sitting at the front of
# sys.path used to shadow the stdlib module entirely. Credentials now come from the
# environment (TG_BOT_TOKEN / TG_CHAT_ID) via vendor/telegram.py, so no such file exists.

_STATE_FILE = config.PENDING_CONFIRMATIONS_PATH


def _atomic_write(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _load():
    if not os.path.exists(_STATE_FILE):
        return {}
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# State that belongs to no particular posting — the digest send time, for instance —
# must not be mixed in with the records (every traversal treats a dict as a posting).
# It all lives under one reserved key.
_META_KEY = "__meta__"


def all_records():
    """Posting records only — the meta key excluded. The large body (jd_text) lives in a
    blob file, so what sits here is only a `{"_blob": true}` placeholder (get() merges
    it back in)."""
    return {k: v for k, v in _load().items() if k != _META_KEY and isinstance(v, dict)}


def get_meta(key, default=None):
    return (_load().get(_META_KEY) or {}).get(key, default)


def set_meta(key, value):
    state = _load()
    meta = state.setdefault(_META_KEY, {})
    meta[key] = value
    _atomic_write(_STATE_FILE, state)


def update(short_id, **fields):
    """Append fields to a record — the path for after-the-fact information such as the
    pre-fit assessment (prefit), the digest send time, or an intermediate result kept
    for a retry. An unknown id is ignored quietly (it may have expired)."""
    state = _load()
    record = state.get(short_id)
    if not isinstance(record, dict):
        return False
    record.update(_split_blobs(short_id, fields))
    _atomic_write(_STATE_FILE, state)
    return True


def is_expired(record, now=None):
    """Public alias — used by the digest and the handlers to filter out expired records."""
    return _is_expired(record, now)


def _dedup_key(record):
    """The key that decides whether two entries are the same posting. Company and role
    spellings wobble between sources, so they are normalized before comparison — the
    name the scraper handed over and the name the LLM pulled out of the JD differed
    often enough that 4 of 20 pending entries were duplicate pairs (a short brand name
    against its expanded form, the same company with and without `Corporation`, an
    apostrophe present in one spelling and dropped in the other). A URL, when present,
    is the most reliable signal, so it wins."""
    url = (record.get("url") or "").strip().lower().rstrip("/")
    if url:
        return ("url", url)
    return ("name", _norm_company(record.get("company")), _norm_text(record.get("role")))


def _norm_text(v):
    # Hangul is kept deliberately: company and role names arrive in whatever
    # language the posting used, and stripping them would collapse every
    # non-Latin name to an empty string, making unrelated postings look like
    # duplicates of each other.
    return re.sub(r"[^a-z0-9가-힣]", "", (v or "").lower())


# Legal-entity suffixes come and go depending on the source — "Acme Robotics" and
# "Acme Robotics Corporation" have to read as the same company.
_COMPANY_SUFFIX_RE = re.compile(
    r"(corporation|corporate|incorporated|company|holdings|group|"
    r"corp|inc|llc|ltd|plc|gmbh|사|주식회사)$"
)


def _norm_company(v):
    norm = _norm_text(v)
    for _ in range(3):  # For stacked suffixes, as in "Acme Inc Holdings"
        stripped = _COMPANY_SUFFIX_RE.sub("", norm)
        if stripped == norm or not stripped:
            break
        norm = stripped
    return norm


def find_duplicate(record):
    """The short_id of a live pending entry for the same posting, or None."""
    key = _dedup_key(record)
    for short_id, existing in all_records().items():
        if _is_expired(existing):
            continue
        if _dedup_key(existing) == key:
            return short_id
    return None


_BLOB_KEYS = ("jd_text",)


def _blob_path(short_id):
    return os.path.join(os.path.dirname(_STATE_FILE), f"pending_jd_{short_id}.txt")


def _split_blobs(short_id, record):
    """Keep the large body out of the index, in a file of its own.

    The index (pending_confirmations.json) is parsed **in full, every time** by get,
    register, discard and prune_expired, and prune_expired runs hourly from the polling
    loop. Measured at 301KB for 20 entries, 42% of it jd_text — every button press was
    parsing 20 JD bodies."""
    record = dict(record)
    for key in _BLOB_KEYS:
        value = record.get(key)
        if isinstance(value, str) and len(value) > 2000:
            try:
                with open(_blob_path(short_id), "w", encoding="utf-8") as f:
                    f.write(value)
                os.chmod(_blob_path(short_id), 0o600)
                record[key] = {"_blob": True}
            except OSError:
                pass  # If splitting fails, leave it in the index as before (still works)
    return record


def _join_blobs(short_id, record):
    record = dict(record)
    for key in _BLOB_KEYS:
        value = record.get(key)
        if isinstance(value, dict) and value.get("_blob"):
            try:
                with open(_blob_path(short_id), encoding="utf-8") as f:
                    record[key] = f.read()
            except OSError:
                record[key] = ""
    return record


def _drop_blobs(short_id):
    for key in _BLOB_KEYS:
        try:
            os.remove(_blob_path(short_id))
        except OSError:
            pass


def register(record):
    """record: company/role/url/jd_profile/keyword_counts/chars_budget/resume_path/result/
    eligibility and whatever else lets rendering continue without calling the LLM again.
    Returns: short_id.

    If the same posting is already pending, **no new entry is made and the existing id
    is returned** — two buttons for one posting can both be pressed, and then the heavy
    pipeline runs twice on the same posting.
    """
    dup = find_duplicate(record)
    if dup:
        return dup
    state = _load()
    short_id = uuid.uuid4().hex[:10]
    entry = {**record, "created_at": datetime.datetime.now().isoformat()}
    state[short_id] = _split_blobs(short_id, entry)
    _atomic_write(_STATE_FILE, state)
    return short_id


def _is_expired(record, now=None):
    """A record with no created_at (one that piled up before the TTL existed) is not
    treated as expired — deleting something whose age is unknown silently throws away a
    request the user may still be waiting on. The first `prune_expired()` run stamps it
    with that moment, and the TTL is counted from there."""
    created = (record or {}).get("created_at")
    if not created:
        return False
    try:
        created_at = datetime.datetime.fromisoformat(created)
    except ValueError:
        return False
    now = now or datetime.datetime.now()
    return (now - created_at).days >= config.CONFIRM_TTL_DAYS


def get(short_id):
    """An expired record is treated as absent — so that pressing an old button before
    prune_expired() has run still gets confirm_handler's "expired / duplicate click"
    message."""
    record = _load().get(short_id)
    if record is None or _is_expired(record):
        return None
    return _join_blobs(short_id, record)


def prune_expired():
    """Delete expired pending requests. Returns: the list of deleted records.

    Why the whole record is returned: a single log line at the call site (bot_listener)
    only needed (company, role), but the expiry is now **booked into the decision
    journal as an implicit no** (see decisions.py). 43 expiries were the strongest
    signal the upstream filter could learn from, and that information was draining away
    as log lines.

    A record with no created_at is not deleted, only stamped with the current time (see
    the _is_expired docstring).
    """
    state = _load()
    dropped, dirty = [], False
    now = datetime.datetime.now()
    for short_id, record in list(state.items()):
        if short_id == _META_KEY or not isinstance(record, dict):
            continue
        if not record.get("created_at"):
            record["created_at"] = now.isoformat()
            dirty = True
        elif _is_expired(record, now):
            dropped.append(record)
            del state[short_id]
            _drop_blobs(short_id)
            dirty = True
    if dirty:
        _atomic_write(_STATE_FILE, state)
    _drop_orphan_blobs(state)
    return dropped


def _drop_orphan_blobs(state):
    """Delete any `pending_jd_*.txt` the index does not know about. Measured once: 31
    records and exactly 31 blob files, so nothing was orphaned — but there are paths
    that leave one behind (a crash before the confirmation request, or a hand edit), and
    a JD body is personal data, so orphans are not left lying around."""
    live = {sid for sid in state if sid != _META_KEY}
    directory = os.path.dirname(_STATE_FILE)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        m = re.fullmatch(r"pending_jd_([0-9a-f]{10})\.txt", name)
        if m and m.group(1) not in live:
            try:
                os.remove(os.path.join(directory, name))
                print(f"[confirm] cleaned up orphan JD body file: {name}")
            except OSError:
                pass


def discard(short_id):
    state = _load()
    if short_id in state:
        del state[short_id]
        _atomic_write(_STATE_FILE, state)
    _drop_blobs(short_id)


def try_claim(short_id):
    """Whether generation may start — only the first caller gets True.

    Press the button twice and the listener starts two threads; the record is deleted
    only **after** generation finishes, so both `get()` calls succeed and the whole
    round trip to the heavy backend (two passes of a ~41KB prompt) runs twice. A mark in
    the file stops the second one.

    Only a mark is written, the record stays — a failure has to be retryable
    (handle_make reverses this with release_claim when it fails).
    """
    state = _load()
    record = state.get(short_id)
    if record is None or record.get("claimed_at"):
        return False
    record["claimed_at"] = datetime.datetime.now().isoformat()
    _atomic_write(_STATE_FILE, state)
    return True


def release_claim(short_id):
    """Reverse the claim when generation failed — it has to be pressable again."""
    state = _load()
    record = state.get(short_id)
    if record and record.pop("claimed_at", None) is not None:
        _atomic_write(_STATE_FILE, state)

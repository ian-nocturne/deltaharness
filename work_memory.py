"""Task-scoped memory ledger and deterministic lexical retrieval.

Connections belong to the caller (sqlite3.Row row_factory). No function commits,
rolls back or opens a connection. Call record in the caller's write transaction.
Times are finite Unix seconds. Source metadata keys contentSha256, contentChars
and observedAt are reserved; caller provenance belongs in other source keys.
"""

import hashlib
import json
import math
import re
import time
import unicodedata


FIELDS = {
    'goal': 'goal',
    'decisions': 'decision',
    'constraints': 'constraint',
    'nextAction': 'next_action',
    'openIssues': 'issue',
}
_KINDS = frozenset(FIELDS.values()) | {'instruction', 'report', 'fact', 'experience', 'hypothesis', 'case'}
_OPERATIONS = frozenset({'assert', 'replace', 'retract', 'resolve'})
_SOURCE_KEYS = frozenset({'contentSha256', 'contentChars', 'observedAt'})
_SCAN_LIMIT = 5000
_EXCERPT_LIMIT = 1200


def _text(value, name, maximum, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{name} must be text of at most {maximum} characters')
    if not empty and not value.strip():
        raise ValueError(f'{name} must not be empty')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValueError(f'{name} contains invalid control characters')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{name} is not valid UTF-8 text') from exc
    return value


def _identifier(value, name):
    value = _text(value, name, 240, empty=False)
    if any(ord(char) < 32 for char in value):
        raise ValueError(f'{name} contains control characters')
    return value


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    return value


def _timestamp(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be finite Unix seconds')
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f'{name} must be finite Unix seconds') from exc
    if not math.isfinite(value):
        raise ValueError(f'{name} must be finite Unix seconds')
    return value


def _json(value, name):
    try:
        # JSON objects have string keys. Reject Python's implicit key coercion
        # (and tuples/custom containers) so different inputs cannot alias.
        def validate(part):
            if part is None or isinstance(part, (bool, int)):
                return
            if isinstance(part, float):
                if not math.isfinite(part):
                    raise ValueError('non-finite number')
                return
            if isinstance(part, str):
                _text(part, name, 16000)
                return
            if isinstance(part, list):
                for element in part:
                    validate(element)
                return
            if isinstance(part, dict):
                for key, element in part.items():
                    if not isinstance(key, str):
                        raise ValueError('non-string object key')
                    _text(key, name, 16000)
                    validate(element)
                return
            raise ValueError('unsupported JSON value')

        validate(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > 16000:
            raise ValueError('encoded source exceeds 16000 bytes')
        return encoded
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError(f'{name} must be bounded valid JSON: {exc}') from exc


def ensure_schema(db):
    """Create only additive memory tables/indexes/triggers; never commit."""
    statements = (
        '''CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
            operation TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            prior_content TEXT, prior_status TEXT,
            revision INTEGER NOT NULL, event_at REAL NOT NULL,
            observed_at REAL NOT NULL, source TEXT NOT NULL,
            event_key TEXT NOT NULL, request_json TEXT NOT NULL,
            UNIQUE(task, event_key))''',
        '''CREATE TABLE IF NOT EXISTS memory_items (
            task TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
            content TEXT NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL, event_at REAL NOT NULL,
            observed_at REAL NOT NULL, source TEXT NOT NULL,
            event_key TEXT NOT NULL, event_id INTEGER NOT NULL,
            PRIMARY KEY(task, item_id))''',
        '''CREATE INDEX IF NOT EXISTS memory_events_task_order
            ON memory_events(task, event_at DESC, id DESC)''',
        '''CREATE INDEX IF NOT EXISTS memory_items_task_status_kind
            ON memory_items(task, status, kind, event_at DESC, event_id DESC)''',
        '''CREATE TRIGGER IF NOT EXISTS memory_events_no_update
            BEFORE UPDATE ON memory_events BEGIN
            SELECT RAISE(ABORT, 'memory events are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS memory_events_no_delete
            BEFORE DELETE ON memory_events BEGIN
            SELECT RAISE(ABORT, 'memory events are immutable'); END''',
    )
    for statement in statements:
        db.execute(statement)
    for table in ('memory_events', 'memory_items'):
        columns = {row[1] for row in db.execute('PRAGMA table_info('+table+')')}
        for name in ('valid_from', 'valid_until'):
            if name not in columns:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' REAL')
    db.execute('CREATE INDEX IF NOT EXISTS memory_events_known ON memory_events(task,observed_at,item_id)')


def _public(row):
    if row is None:
        return None
    result = dict(row)
    result.pop('request_json', None)
    result['source'] = json.loads(result['source'])
    return result


def record(db, task, revision, event_key, kind, item_id, operation,
           content='', source=None, event_at=None, valid_from=None, valid_until=None):
    """Append one event and update its projection in the caller's transaction.

    Retries compare the original arguments, including explicit event_at/source,
    without comparing generated timestamps. A changed retry raises ValueError.
    Retraction/resolution retains the previous text and does not execute work.
    """
    task = _identifier(task, 'task')
    item_id = _identifier(item_id, 'item_id')
    event_key = _identifier(event_key, 'event_key')
    revision = _integer(revision, 'revision', 0, 2**63 - 1)
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError('invalid memory kind')
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError('invalid memory operation')
    if operation == 'resolve' and kind != 'issue':
        raise ValueError('only issues can be resolved')
    content = _text(content, 'content', 100000)
    if source is not None and not isinstance(source, dict):
        raise ValueError('source must be a JSON object or None')
    source_json = _json(source, 'source')
    # Keep the canonical request separate from enriched event provenance.
    requested_time = None if event_at is None else _timestamp(event_at, 'event_at')
    start = None if valid_from is None else _timestamp(valid_from, 'valid_from')
    end = None if valid_until is None else _timestamp(valid_until, 'valid_until')
    if end is not None and start is not None and end <= start:
        raise ValueError('valid_until must be later than valid_from')
    request = [revision, kind, item_id, operation, content, json.loads(source_json), requested_time]
    if start is not None or end is not None:
        request += [start, end]
    request_json = json.dumps(
        request,
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    existing_event = db.execute(
        'SELECT request_json FROM memory_events WHERE task=? AND event_key=?',
        (task, event_key)).fetchone()
    if existing_event is not None:
        if existing_event['request_json'] != request_json:
            raise ValueError('event_key already exists with different payload')
        return False

    before = db.execute('SELECT * FROM memory_items WHERE task=? AND item_id=?',
                        (task, item_id)).fetchone()
    if before is not None and before['kind'] != kind:
        raise ValueError('an item kind cannot change')
    active = before is not None and before['status'] == 'active'
    if operation == 'assert' and active and before['content'] != content:
        raise ValueError('different active content requires replace')
    if operation != 'assert' and not active:
        raise ValueError(f'{operation} requires an existing active item')

    observed_at = _timestamp(time.time(), 'observed_at')
    effective_time = observed_at if requested_time is None else requested_time
    if before is not None and effective_time < before['event_at']:
        raise ValueError('event_at is older than the item update')
    if end is not None and end <= (effective_time if start is None else start):
        raise ValueError('valid_until must be later than the effective start')
    resulting_content = before['content'] if operation in ('retract', 'resolve') else content
    status = {'retract': 'retracted', 'resolve': 'resolved'}.get(operation, 'active')
    enriched_source = json.loads(source_json) if source is not None else {}
    metadata = {
        'contentSha256': hashlib.sha256(resulting_content.encode('utf-8')).hexdigest(),
        'contentChars': len(resulting_content), 'observedAt': observed_at,
    }
    for key in _SOURCE_KEYS:
        if key in enriched_source and enriched_source[key] != metadata[key]:
            raise ValueError(f'source.{key} is reserved memory metadata')
    enriched_source.update(metadata)
    encoded_source = _json(enriched_source, 'source')

    inserted = db.execute('''INSERT INTO memory_events
        (task,item_id,kind,operation,content,status,prior_content,prior_status,
         revision,event_at,observed_at,source,event_key,request_json,valid_from,valid_until)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            task, item_id, kind, operation, resulting_content, status,
            before['content'] if before else None, before['status'] if before else None,
            revision, effective_time, observed_at, encoded_source, event_key, request_json, start, end))
    db.execute('''INSERT INTO memory_items
        (task,item_id,kind,content,status,revision,event_at,observed_at,source,event_key,event_id,valid_from,valid_until)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task,item_id) DO UPDATE SET
        content=excluded.content,status=excluded.status,revision=excluded.revision,
        event_at=excluded.event_at,observed_at=excluded.observed_at,
        source=excluded.source,event_key=excluded.event_key,event_id=excluded.event_id,
        valid_from=excluded.valid_from,valid_until=excluded.valid_until''', (
            task, item_id, kind, resulting_content, status, revision, effective_time,
            observed_at, encoded_source, event_key, inserted.lastrowid, start, end))
    return True


def history(db, task, limit=30):
    """Return newest task events, including previous and resulting values."""
    task = _identifier(task, 'task')
    limit = _integer(limit, 'limit', 0, 1000)
    return [_public(row) for row in db.execute(
        'SELECT * FROM memory_events WHERE task=? ORDER BY event_at DESC,id DESC LIMIT ?',
        (task, limit))]


def snapshot(db, task, at=None, known_at=None, include_inactive=False,
             kinds=None, limit=None, before=None, after=None, project_only=False, with_count=False):
    """Bitemporal state, with bounded SQL paging for large conversation histories."""
    task = _identifier(task, 'task')
    at = time.time() if at is None else _timestamp(at, 'at')
    known_at = time.time() if known_at is None else _timestamp(known_at, 'known_at')
    clauses=['task=?','observed_at<=?','COALESCE(valid_from,event_at)<=?'];params=[task,known_at,at]
    if kinds is not None:
        if not isinstance(kinds,(list,tuple,set,frozenset)) or not kinds or any(k not in _KINDS for k in kinds):
            raise ValueError('invalid memory kinds')
        kinds=sorted(kinds);clauses.append('kind IN ('+','.join('?' for _ in kinds)+')');params.extend(kinds)
    base='SELECT *,ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY observed_at DESC,id DESC) AS position FROM memory_events WHERE '+' AND '.join(clauses)
    outer=['position=1']
    if not include_inactive:
        outer += ["status='active'",'(valid_until IS NULL OR valid_until>?)'];params.append(at)
    if before is not None:outer.append('event_at<?');params.append(_timestamp(before,'before'))
    if after is not None:outer.append('event_at>=?');params.append(_timestamp(after,'after'))
    if project_only:outer.append("json_extract(source,'$.scope')='project' AND json_extract(source,'$.shareEvidence') IS NOT NULL")
    base=' FROM ('+base+') WHERE '+' AND '.join(outer)
    total=db.execute('SELECT count(*)'+base,params).fetchone()[0] if with_count else None
    suffix=' ORDER BY event_at DESC,id DESC' if limit is not None else ' ORDER BY kind,item_id'
    if limit is not None:
        limit=_integer(limit,'limit',0,5000);suffix+=' LIMIT ?';params.append(limit)
    rows=db.execute('SELECT *'+base+suffix,params).fetchall()
    result=[]
    for row in rows:
        item=_public(row);item.pop('position',None);item['event_id']=item.pop('id')
        item['effectiveStatus']=('expired' if item.get('valid_until') is not None and at>=item['valid_until'] else item['status'])
        result.append(item)
    return {'items':result,'count':total} if with_count else result


def current(db, task, kinds=None, at=None, known_at=None):
    return snapshot(db,task,at,known_at,kinds=kinds)


def get_item(db, task, item_id):
    """Return an item's latest projection, including inactive entries."""
    task = _identifier(task, 'task')
    item_id = _identifier(item_id, 'item_id')
    return _public(db.execute('SELECT * FROM memory_items WHERE task=? AND item_id=?',
                              (task, item_id)).fetchone())


def _normalize(text):
    return unicodedata.normalize('NFC', text).casefold()


def _lexical(content, query, terms, bigrams):
    normalized = _normalize(content)
    score, anchor = 0, None
    if query and query in normalized:
        score, anchor = 100, normalized.index(query)
    for term in terms:
        index = normalized.find(term)
        if index >= 0:
            score += 20
            if anchor is None:
                anchor = index
    matches = [(normalized.find(pair), pair) for pair in bigrams]
    matches = [match for match in matches if match[0] >= 0]
    if matches:
        score += round(10 * len(matches) / len(bigrams), 6)
        if anchor is None:
            anchor = min(index for index, _ in matches)
    return score, anchor


def _original_offset(content, normalized_offset):
    # Only selected passages need offset translation. NFC/casefold can change
    # character counts; returned excerpt offsets always address original text.
    low, high = 0, len(content)
    while low < high:
        middle = (low + high) // 2
        if len(_normalize(content[:middle])) <= normalized_offset:
            low = middle + 1
        else:
            high = middle
    return max(0, low - 1)


def retrieve(db, task, query='', budget=6000, limit=8):
    """Retrieve active task instruction/report passages within a char budget.

    At most the latest 5000 candidates are scanned. candidateCount counts all
    eligible rows and omitted counts every candidate not returned (including
    duplicates, nonmatches, scan limits and budget/limit exclusions). truncated
    reports either omitted candidates or shortened selected passages.
    """
    task = _identifier(task, 'task')
    query = _text(query, 'query', 2000)
    budget = _integer(budget, 'budget', 0, 100000)
    limit = _integer(limit, 'limit', 0, 100)
    normalized_query = _normalize(query).strip()
    terms = sorted(set(re.findall(r'[^\W_]+', normalized_query)))
    # Korean substring searches also match spacing/compound-word variations.
    bigrams = sorted({term[index:index + 2] for term in terms
                      if any('\uac00' <= char <= '\ud7a3' for char in term)
                      for index in range(len(term) - 1)})
    where = "task=? AND status='active' AND kind IN ('instruction','report')"
    candidate_count = db.execute(f'SELECT COUNT(*) AS total FROM memory_items WHERE {where}',
                                 (task,)).fetchone()['total']
    rows = db.execute(f'''SELECT * FROM memory_items WHERE {where}
        ORDER BY event_at DESC,observed_at DESC,event_id DESC LIMIT ?''',
                      (task, _SCAN_LIMIT)).fetchall()
    ranked = []
    for row in rows:
        score, anchor = _lexical(row['content'], normalized_query, terms, bigrams)
        if normalized_query and not score:
            continue
        ranked.append((score, row, anchor))
    ranked.sort(key=lambda entry: (-entry[0], -entry[1]['event_at'],
                                  -entry[1]['observed_at'], -entry[1]['event_id']))
    items, seen, remaining, shortened = [], set(), budget, False
    for score, row, anchor in ranked:
        if len(items) >= limit or remaining <= 0:
            break
        content = row['content']
        if content in seen:
            continue
        seen.add(content)
        maximum = min(_EXCERPT_LIMIT, remaining)
        original_anchor = _original_offset(content, anchor) if anchor is not None else 0
        start = max(0, min(original_anchor - maximum // 3, len(content) - maximum))
        end = min(len(content), start + maximum)
        item = _public(row)
        item.pop('content')
        item.update(excerpt=content[start:end], excerptStart=start, excerptEnd=end,
                    contentChars=len(content), score=score)
        items.append(item)
        remaining -= end - start
        shortened = shortened or start != 0 or end != len(content)
    omitted = candidate_count - len(items)
    return {'items': items, 'omitted': omitted, 'truncated': bool(omitted or shortened),
            'candidateCount': candidate_count, 'scannedCount': len(rows)}

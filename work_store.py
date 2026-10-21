"""Task continuity, independent of native session ownership. No model calls."""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import math
import logging
from config_state import update_config
import work_memory
import memory_intelligence
import memory_skills
from contextlib import contextmanager
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text(value, limit=8000, required=False):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('입력 길이와 형식을 확인해 주세요.')
    value = value.strip()
    if required and not value:
        raise ValueError('필수 항목을 입력해 주세요.')
    return value


class WorkStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'work_tasks.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project TEXT NOT NULL,title TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(task TEXT NOT NULL,session TEXT NOT NULL,role TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(task,session));
                CREATE INDEX IF NOT EXISTS sessions_session ON sessions(session);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,task TEXT NOT NULL,session TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task TEXT NOT NULL,event_key TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,UNIQUE(task,event_key));
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY,task TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(session TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS input_leases(task TEXT PRIMARY KEY,owner TEXT NOT NULL,token TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_seeds(task TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS memory_batches(task TEXT NOT NULL,operation TEXT NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(task,operation));
            ''')
            work_memory.ensure_schema(db)
            memory_intelligence.ensure_schema(db)
        # Migrate only the existing task summary, never crawl conversation logs.
        # The projection is additive and does not change task revision/ownership.
        with self.db(True) as db:
            for row in db.execute('SELECT * FROM tasks').fetchall():
                self._seed_memory(db, self.row(row))

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def row(row):
        if row is None:
            return None
        result = dict(row)
        if 'body' in result:
            result['body'] = json.loads(result['body'])
        return result

    def get(self, task):
        with self.db() as db:
            row = self.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
            if row:
                core=work_memory.current(db,task,list(work_memory.FIELDS.values()))
                row['body']={**row['body'],**memory_intelligence.effective_fields({'current':core})}
        if row is None:
            raise ValueError('업무를 찾지 못했어요.')
        return row

    def _seed_memory(self, db, item):
        tid = item['id']
        if db.execute('SELECT 1 FROM memory_seeds WHERE task=?', (tid,)).fetchone():
            return
        body = item['body']
        source = {'kind':'legacy-snapshot', 'reference':str(self.path),
                  'sourceFiles':body.get('sourceFiles', [])[:20]}
        for field, kind in work_memory.FIELDS.items():
            if body.get(field):
                work_memory.record(db, tid, item['revision'], 'legacy:'+field, kind,
                    'field:'+field, 'assert', body[field], {**source,'field':field}, 0)
        for field, kind in (('lastRequest','instruction'), ('latestResult','report')):
            if body.get(field):
                work_memory.record(db, tid, item['revision'], 'legacy:'+field, kind,
                    'legacy:'+field, 'assert', body[field], {**source,'field':field},
                    body.get('lastHumanAt',0) if kind == 'instruction' else 0)
        db.execute('INSERT INTO memory_seeds VALUES(?)', (tid,))

    def _replace_memory_field(self, db, current, field, value, source, event_at):
        """Replace one compatibility field while retaining individual item history."""
        tid = current['id']; kind = work_memory.FIELDS[field]
        target = 'field:'+field; revision = current['revision']+1
        for row in db.execute("SELECT * FROM memory_items WHERE task=? AND kind=? AND status='active'",(tid,kind)).fetchall():
            item=work_memory._public(row)
            if item['kind'] == kind and (item['item_id'] != target or not value):
                key = 'field:'+fingerprint([revision,field,item['item_id'],'retract'])
                work_memory.record(db, tid, revision, key, kind, item['item_id'],
                                   'retract', source=source, event_at=event_at)
        if value:
            old = work_memory.get_item(db, tid, target)
            if (old and old['status'] == 'active' and old['content'] == value
                    and (old.get('valid_from') is None or old['valid_from']<=time.time())
                    and (old.get('valid_until') is None or old['valid_until']>time.time())):
                return
            operation = 'replace' if old and old['status'] == 'active' else 'assert'
            work_memory.record(db, tid, revision, 'field:'+fingerprint([revision,field,value]),
                kind, target, operation, value, source, event_at)

    def memory(self, task, query='', budget=6000, include_retrieval=True, **options):
        if set(options)-{'at','known_at','before','after','shared','federated','limit'}:
            raise ValueError('invalid memory query options')
        index_result=None
        if include_retrieval and query and memory_intelligence.vectors._MODEL is not None:
            index_result=memory_intelligence.index_task(self,task)
        with self.db() as db:
            db.execute('BEGIN')
            row=db.execute('SELECT revision FROM tasks WHERE id=?',(task,)).fetchone()
            if not row: raise ValueError('업무를 찾지 못했어요.')
            view = self._memory_view(db,task,query,budget,include_retrieval,**options)
            rev = row['revision']
        ret = view.get('retrieval') or {}
        if (index_result is None and include_retrieval and query
                and memory_intelligence.vectors._MODEL is not None
                and ret.get('semanticIndexedCandidates', 0) == 0
                and not ret.get('semanticError')):
            index_result = memory_intelligence.index_task(self, task)
            with self.db() as db:
                db.execute('BEGIN')
                row = db.execute('SELECT revision FROM tasks WHERE id=?', (task,)).fetchone()
                view = self._memory_view(db, task, query, budget, include_retrieval, **options)
                rev = row['revision']
        return {**view, 'revision': rev, 'indexing': index_result}

    def heal_skills(self, task):
        return memory_intelligence.heal_skills(self, task)

    def memory_capsule(self, task, plan='', files=None, worker='', budget=1200):
        return memory_intelligence.build_subagent_capsule(self, task, plan=plan, files=files, worker=worker, budget=budget)

    def skills(self, task, query='', include_inactive=False, **options):
        if set(options)-{'at','known_at','before','after','shared','federated','limit','budget'}:
            raise ValueError('invalid skill query options')
        at=memory_intelligence.timestamp(options.get('at'),'at')
        known=memory_intelligence.timestamp(options.get('known_at'),'known_at')
        before=memory_intelligence.timestamp(options.get('before'),'before')
        after=memory_intelligence.timestamp(options.get('after'),'after')
        with self.db() as db:
            row=db.execute('SELECT revision FROM tasks WHERE id=?',(task,)).fetchone()
            if not row: raise ValueError('업무를 찾지 못했어요.')
            items=memory_skills.snapshot_skills(
                db,task,at=at,known_at=known,before=before,after=after,
                shared=bool(options.get('shared',False)),
                include_inactive=bool(include_inactive),limit=options.get('limit',200),
            )
            retrieval=memory_skills.retrieve_skills(
                db,task,query=text(query,2000),budget=options.get('budget',2400),
                limit=min(50,options.get('limit',8)),root=self.root,
                at=at,known_at=known,before=before,after=after,
                shared=bool(options.get('shared',False)),
                federated=bool(options.get('federated',False)),
                semantic_config=memory_intelligence.settings(self.root),
            ) if query else None
            return {'taskId':task,'revision':row['revision'],'skills':items,
                    'history':memory_skills.skill_events(db,task,limit=30),'retrieval':retrieval}

    def _memory_view(self, db, task, query='', budget=6000, include_retrieval=True, **options):
        query=text(query,100000)
        retrieval=memory_intelligence.retrieve(db,task,query[:2000],budget,root=self.root,**options) if include_retrieval else None
        if retrieval is not None:
            retrieval['queryOmittedChars']=max(0,len(query)-2000)
        at=memory_intelligence.timestamp(options.get('at'),'at')
        known=memory_intelligence.timestamp(options.get('known_at'),'known_at')
        active=work_memory.current(db,task,kinds=list(work_memory.FIELDS.values())+['fact','experience','case','hypothesis'],at=at,known_at=known)
        return {'version':3, 'taskId':task,
                'current':[r for r in active if r['kind'] in work_memory.FIELDS.values()],
                'facts':[r for r in active if r['kind']=='fact'],
                'experiences':[r for r in active if r['kind']=='experience'],
                'cases':[r for r in active if r['kind']=='case'],
                'hypotheses':[r for r in active if r['kind']=='hypothesis'],
                'skills':memory_skills.snapshot_skills(db,task,at=at,known_at=known),
                'skillHistory':memory_skills.skill_events(db,task,limit=20),
                'links':memory_skills.get_links(db,task,limit=80),
                'history':work_memory.history(db,task,30),
                'retrieval':retrieval}

    def patch_memory(self, task, revision, operation, patches):
        """Explicit, revision-checked item edits; never infer execution approval."""
        text(operation,128,True)
        if not isinstance(patches,list) or not 1 <= len(patches) <= 30:
            raise ValueError('기억 변경 항목은 1~30개로 지정해 주세요.')
        payload = fingerprint({'revision':revision,'patches':patches})
        with self.db(True) as db:
            prior = db.execute('SELECT * FROM memory_batches WHERE task=? AND operation=?',
                               (task,operation)).fetchone()
            if prior:
                if prior['digest'] != payload: raise ValueError('같은 기억 요청의 내용이 바뀌었어요.')
                return json.loads(prior['result'])
            current = self.row(db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
            if not current or current['revision'] != revision:
                raise ValueError('업무가 갱신됐어요. 최신 내용을 확인해 주세요.')
            self._check_input_lease(db,task)
            touched=set(); now=time.time(); body=current['body']
            source={'kind':'explicit-memory-edit','session':current['owner'],'operation':operation}
            for i, patch in enumerate(patches):
                if not isinstance(patch,dict) or set(patch)-{'id','kind','op','content','evidence','validFrom','validUntil'}:
                    raise ValueError('기억 변경 형식을 확인해 주세요.')
                kind=patch.get('kind'); op=patch.get('op')
                if kind not in work_memory.FIELDS.values():
                    raise ValueError('목표·결정·제약·다음 행동·확인 항목만 수정할 수 있어요.')
                value=text(patch.get('content',''),8000,op in ('assert','replace'))
                evidence=text(patch.get('evidence',''),2000)
                work_memory.record(db,task,revision+1,'patch:'+fingerprint([operation,i]),kind,
                    patch.get('id'),op,value,{**source,'evidence':evidence},now,
                    valid_from=memory_intelligence.timestamp(patch.get('validFrom'),'validFrom'),
                    valid_until=memory_intelligence.timestamp(patch.get('validUntil'),'validUntil'))
                touched.add(kind)
            for kind in touched:
                total=db.execute("SELECT COALESCE(sum(length(content)+1),0) FROM memory_items WHERE task=? AND kind=? AND status='active' AND (valid_until IS NULL OR valid_until>?)",(task,kind,now)).fetchone()[0]
                if total>8001:raise ValueError('현재·예정 핵심 기억은 종류별 8,000자 이내로 정리해 주세요.')
            active=work_memory.current(db,task,list(touched))
            manual=body.setdefault('_manualFields',{})
            for field,kind in work_memory.FIELDS.items():
                if kind in touched:
                    combined='\n'.join(r['content'] for r in active if r['kind']==kind)
                    body[field]=text(combined,8000); manual[field]=True
            db.execute('UPDATE tasks SET body=?,revision=revision+1,updated=? WHERE id=?',
                       (encoded(body),now,task))
            result={'taskId':task,'revision':revision+1,'operationId':operation,'changedKinds':sorted(touched)}
            db.execute('INSERT INTO memory_batches VALUES(?,?,?,?)',(task,operation,payload,encoded(result)))
        return result

    def acquire_input(self, task, owner, token):
        with self.db(True) as db:
            row=db.execute('SELECT owner FROM tasks WHERE id=?',(task,)).fetchone()
            if not row or row['owner']!=owner:
                raise ValueError('담당자가 바뀌어 입력을 보내지 않았어요. 현재 담당을 다시 확인해 주세요.')
            lease=db.execute('SELECT * FROM input_leases WHERE task=?',(task,)).fetchone()
            if lease and lease['expires']>time.time() and lease['token']!=token:
                raise ValueError('이 업무의 다른 입력을 처리 중이에요. 잠시 뒤 보내 주세요.')
            if lease and lease['expires']>time.time() and lease['token']==token:
                return False
            db.execute('INSERT OR REPLACE INTO input_leases VALUES(?,?,?,?)',(task,owner,token,time.time()+600))
            return True

    def release_input(self, task, token):
        with self.db(True) as db:
            db.execute('DELETE FROM input_leases WHERE task=? AND token=?',(task,token))

    def _check_input_lease(self, db, task, token=''):
        row=db.execute('SELECT * FROM input_leases WHERE task=? AND expires>?',(task,time.time())).fetchone()
        if row and row['token']!=token:
            raise ValueError('입력을 전달 중이어서 담당 변경을 보류했어요.')

    def for_session(self, session):
        with self.db() as db:
            rows = db.execute('SELECT t.* FROM tasks t JOIN sessions s ON s.task=t.id WHERE s.session=? ORDER BY t.updated DESC', (session,)).fetchall()
        return [self.row(row) for row in rows]

    def list(self):
        with self.db() as db:
            return [self.row(r) for r in db.execute('SELECT * FROM tasks ORDER BY updated DESC LIMIT 500')]

    def links(self, task):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM sessions WHERE task=? ORDER BY created,session', (task,))]

    def create(self, project, title, owner='', goal='', task=None, source='user'):
        project = text(project, 120, True)
        title = text(title, 240, True)
        goal = text(goal)
        task = text(task or 'task-' + uuid.uuid4().hex, 240, True)
        with self.db(True) as db:
            inserted = db.execute('INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                       (task, project, title, owner, 'active', 1,
                        encoded({'goal': goal, 'decisions': '', 'constraints': '', 'nextAction': '', 'openIssues': '', 'evidence': [], 'source': source}), time.time()))
            if owner and inserted.rowcount:
                self._link(db, task, owner, 'responsible', source)
            if inserted.rowcount:
                self._seed_memory(db, self.row(db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()))
        return self.get(task)

    def ensure_standalone_task(self, task, session='standalone-owner', project='', title='', goal=''):
        """Bootstrap a standalone task only if it does not exist; never alter an existing task."""
        task = text(task or 'standalone/main', 240, True)
        try:
            return self.get(task)
        except ValueError:
            inferred_project = text(project or (task.split('/', 1)[0] if '/' in task else 'standalone'), 120, True)
            inferred_title = text(title or (task.split('/', 1)[1] if '/' in task else task), 240, True)
            owner = text(session or 'standalone-owner', 240, True)
            return self.create(inferred_project, inferred_title, owner=owner, goal=goal, task=task, source='standalone-mcp')

    def _link(self, db, task, session, role, evidence):
        text(session, 240, True)
        if role not in ('responsible', 'previous', 'worker', 'reviewer', 'reference'):
            raise ValueError('세션 역할을 확인해 주세요.')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?) ON CONFLICT(task,session) DO UPDATE SET role=excluded.role,evidence=excluded.evidence',
                   (task, session, role, text(evidence, 1000, True), time.time()))

    def link(self, task, session, role='reference', evidence='user-link'):
        self.get(task)
        if role == 'responsible':
            raise ValueError('책임자 변경은 담당 교대에서 진행해 주세요.')
        with self.db(True) as db:
            if db.execute('SELECT owner FROM tasks WHERE id=?', (task,)).fetchone()[0] == session:
                raise ValueError('현재 책임자의 역할은 담당 교대에서 변경해 주세요.')
            self._link(db, task, session, role, evidence)
        return self.get(task)

    def update(self, task, revision, values):
        allowed = {'goal', 'decisions', 'constraints', 'nextAction', 'openIssues', 'state', 'title', 'evidence'}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError('저장 항목을 확인해 주세요.')
        for key, value in values.items():
            if key != 'evidence':
                text(value, 8000)
        if 'evidence' in values and (not isinstance(values['evidence'], list) or len(values['evidence']) > 30):
            raise ValueError('근거는 30개 이내로 연결해 주세요.')
        for item in values.get('evidence', []):
            text(item, 2000, True)
        with self.db(True) as db:
            current = self.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
            if not current or current['revision'] != revision:
                raise ValueError('업무가 갱신됐어요. 최신 내용을 확인해 주세요.')
            state = values.get('state', current['state'])
            if state not in ('active', 'input', 'review', 'blocked', 'paused', 'done'):
                raise ValueError('업무 상태를 확인해 주세요.')
            if state == 'done':
                pending = db.execute("SELECT count(*) FROM attempts WHERE task=? AND state IN ('sending','unknown','accepted','running','input')", (task,)).fetchone()[0]
                unreviewed = any(json.loads(r[0]).get('reviewState') == 'pending' for r in db.execute('SELECT body FROM attempts WHERE task=?', (task,)))
                if pending or unreviewed:
                    raise ValueError('실행 또는 접수 확인이 남아 있어요.')
                if not (values.get('evidence') or current['body'].get('evidence')):
                    raise ValueError('완료 근거를 연결해 주세요.')
            body = {**current['body'], **{k: v for k, v in values.items() if k not in ('state', 'title')}}
            for field in work_memory.FIELDS:
                if field in values:
                    body[field]=text(values[field])
                    # The existing editor submits all fields. Saving an unchanged
                    # value must not disable future automatic updates to it.
                    if body[field] != current['body'].get(field,''):
                        body.setdefault('_manualFields',{})[field]=True
                    self._replace_memory_field(db,current,field,body[field],
                        {'kind':'explicit-context-edit','session':current['owner']},time.time())
            db.execute('UPDATE tasks SET title=?,state=?,body=?,revision=revision+1,updated=? WHERE id=?',
                       (values.get('title', current['title']), state, encoded(body), time.time(), task))
        return self.get(task)

    def sync_context(self, task, values, source, owner='', activity=None, source_at=None):
        """Project evidence into a work view; never change execution/approval state.

        Explicit user edits win over automatic fields. A newly bound conversation
        can supply the display owner only when no owner has been recorded yet.
        """
        if source_at is not None and (isinstance(source_at,bool) or not isinstance(source_at,(int,float)) or not math.isfinite(source_at) or source_at < 0):
            raise ValueError('기억 원본의 시각을 확인해 주세요.')
        with self.db(True) as db:
            current=self.row(db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
            if not current: raise ValueError('업무를 찾지 못했어요.')
            # Passive projections are replayed by the existing observation loop.
            # Do not invalidate a handoff snapshot while its input is in flight.
            try:self._check_input_lease(db,task)
            except ValueError:return current
            body=current['body']; before=encoded(body)
            if '_auto' not in body and body.get('source') in ('nomad-binding','user-approved-20261018','exact-conversation'):
                body['_auto']={k:body.get(k) for k in ('goal','decisions','constraints','nextAction','openIssues')}
            automatic=body.setdefault('_auto',{})
            clocks=body.setdefault('_sourceClock',{})
            for key,value in values.items():
                # Omitted key means unchanged; an explicit empty string clears it.
                if key not in work_memory.FIELDS: continue
                value=text(value,8000)
                if body.get('_manualFields',{}).get(key): continue
                if source_at is not None and source_at < clocks.get(key,0): continue
                if not body.get(key) or body.get(key)==automatic.get(key):
                    if body.get(key) != value:
                        self._replace_memory_field(db,current,key,value,
                            {'kind':'source-projection','reference':source,
                             'sourceFiles':(activity or {}).get('sourceFiles',body.get('sourceFiles',[]))[:20]},
                            source_at if source_at is not None else time.time())
                    body[key]=value;automatic[key]=value
                    if source_at is not None: clocks[key]=source_at
            if activity:
                body.update(activity)
            body['contextSource']=source
            already_owns_active = bool(owner and db.execute(
                "SELECT 1 FROM tasks WHERE owner=? AND id!=? AND id!='test' AND state NOT IN ('done','paused')",
                (owner, task),
            ).fetchone())
            adopted=bool(owner and not current['owner'] and not already_owns_active)
            if adopted: self._link(db,task,owner,'responsible','conversation-reference:'+source)
            if before!=encoded(body) or adopted:
                db.execute('UPDATE tasks SET body=?,owner=?,revision=revision+1,updated=? WHERE id=?',
                           (encoded(body),owner if adopted else current['owner'],time.time(),task))
        return self.get(task)

    def event(self, task, key, kind, body):
        with self.db(True) as db:
            db.execute('INSERT OR IGNORE INTO events(task,event_key,kind,body,created) VALUES(?,?,?,?,?)',
                       (task, key, kind, encoded(body), time.time()))

    def _sync_config_owner_handover(self, previous, successor):
        if not successor or previous == successor:
            return
        cfg_path = self.root / 'config.json'
        if not cfg_path.exists():
            return
        def mutate(cfg):
            changed = False
            pinned = list(cfg.get('pinned_sessions') or [])
            if previous and previous in pinned:
                # Keep the previous owner's slot even if the successor was
                # already pinned elsewhere; remove duplicate IDs.
                pinned = list(dict.fromkeys(successor if sid == previous else sid
                                            for sid in pinned if sid != successor))
                cfg['pinned_sessions'] = pinned
                changed = True
            titles = cfg.get('session_titles')
            if isinstance(titles, dict) and previous and previous in titles:
                cleaned = re.sub(r'\s*(?:\(\s*주\s*책임(?:자)?\s*\)|·\s*주\s*책임(?:자)?|·\s*담당|\(\s*담당\s*\)|주책임(?:자)?)\s*$', '', str(titles[previous])).strip()
                if cleaned and cleaned != titles[previous]:
                    titles[previous] = cleaned
                    changed = True
            return changed
        try:
            update_config(cfg_path, mutate)
        except Exception:
            logging.exception("Owner recorded, but pin/title projection failed")

    def responsibility_rows(self):
        """Read owners and previous-session links from one SQLite snapshot."""
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT t.id,t.revision,t.owner,t.state,t.title,s.session AS previous
                FROM tasks t LEFT JOIN sessions s ON s.task=t.id AND s.role='previous'
                WHERE t.owner!='' AND t.state NOT IN ('done','paused')
                ORDER BY t.id,s.session
            """)]

    def reconcile_owner(self, task, expected_owner, owner, evidence, operation):
        """Apply an evidenced owner correction once, without executing any work."""
        text(owner,240,True); text(evidence,1000,True); text(operation,240,True)
        with self.db(True) as db:
            if db.execute('SELECT 1 FROM events WHERE task=? AND event_key=?',(task,operation)).fetchone():
                return False
            self._check_input_lease(db,task)
            row=db.execute('SELECT owner FROM tasks WHERE id=?',(task,)).fetchone()
            if not row or row[0] != expected_owner:
                raise ValueError('주책임자가 바뀌었어요. 최신 인계 근거를 확인해 주세요.')
            if expected_owner and expected_owner != owner:
                self._link(db,task,expected_owner,'previous',evidence)
            self._link(db,task,owner,'responsible',evidence)
            db.execute('UPDATE tasks SET owner=?,revision=revision+1,updated=? WHERE id=?',(owner,time.time(),task))
            db.execute('INSERT INTO events(task,event_key,kind,body,created) VALUES(?,?,?,?,?)',
                       (task,operation,'owner_reconciled',encoded({'previous':expected_owner,'owner':owner,'evidence':evidence,'executionTransferred':False}),time.time()))
        self._sync_config_owner_handover(expected_owner, owner)
        return True

    def events(self, task):
        with self.db() as db:
            return [self.row(r) for r in db.execute('SELECT * FROM events WHERE task=? ORDER BY seq DESC LIMIT 100', (task,))]

    def observations(self):
        with self.db() as db:
            return {r['session']:self.row(r) for r in db.execute('SELECT * FROM observations')}

    def claim(self, operation, payload):
        if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', operation):
            raise ValueError('요청 번호가 없어요. 화면을 다시 열어 주세요.')
        digest = fingerprint(payload)
        with self.db(True) as db:
            old = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('같은 요청 번호의 내용이 바뀌었어요.')
                return False, json.loads(old['result'])
            result = {'ok': False, 'state': 'unknown', 'operationId': operation,
                      'error': '이 요청의 진행 상태를 확인해 주세요. 자동으로 재전송하지 않아요.'}
            db.execute('INSERT INTO operations VALUES(?,?,?,?)', (operation, digest, 'sending', encoded(result)))
        return True, None

    def operation(self, operation, payload):
        with self.db() as db:
            row = db.execute('SELECT digest,result FROM operations WHERE id=?', (operation,)).fetchone()
        if row:
            if row['digest'] != fingerprint(payload):
                raise ValueError('같은 요청 번호의 내용이 바뀌었어요. 기존 결과를 확인해 주세요.')
            return json.loads(row['result'])

    def finish(self, operation, result):
        with self.db(True) as db:
            db.execute('UPDATE operations SET state=?,result=? WHERE id=?', (result.get('state', 'done'), encoded(result), operation))
        return result

    def attempt(self, task, kind, body, operation, resume_capacity=False):
        aid = 'attempt-' + fingerprint([task, operation])[:32]
        with self.db(True) as db:
            for raw in db.execute("SELECT * FROM attempts WHERE task=? AND state IN ('sending','unknown','accepted','running','input')", (task,)):
                previous = self.row(raw)
                if previous['id'] == aid:
                    return aid
                if kind in ('handover','start') or previous['kind'] in ('handover','start') or previous['state'] in ('sending','unknown') or previous['body'].get('intentHash') == body.get('intentHash'):
                    raise ValueError('이 업무에 실행 또는 접수 확인이 남아 있어요. 기존 기록을 먼저 확인해 주세요.')
            db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?,?,?,?,?,?)',
                       (aid, task, '', kind, 'sending', encoded(body), time.time(), time.time()))
            # Only a gate's recorded zero-start wait may reuse this operation.
            # Other rejections, native sessions and unknown outcomes stay intact.
            if resume_capacity:
                db.execute("UPDATE attempts SET state='sending',body=?,updated=? WHERE id=? AND state='not_sent' AND session='' AND json_extract(body,'$.capacityDeferred')=1",
                           (encoded(body), time.time(), aid))
        return aid

    def review(self, task, aid, decision, note):
        if decision not in ('adopted', 'revision_requested', 'rejected'):
            raise ValueError('검토 결과를 선택해 주세요.')
        text(note, 8000, True)
        with self.db(True) as db:
            row = self.row(db.execute('SELECT * FROM attempts WHERE id=? AND task=?', (aid, task)).fetchone())
            if not row or row['state'] != 'completed' or not row['body'].get('responseHash'):
                raise ValueError('완료 응답이 확인된 실행만 검토할 수 있어요.')
            if decision == 'adopted' and (row['body'].get('fallback') or {}).get('draftOnly'):
                raise ValueError('이 결과는 대체 초안이에요. 정확한 모델의 검토 결과를 별도로 받아 주세요.')
            body = {**row['body'], 'reviewState': decision, 'reviewNote': note, 'reviewedAt': time.time()}
            db.execute('UPDATE attempts SET body=?,updated=? WHERE id=?', (encoded(body), time.time(), aid))
            db.execute('UPDATE tasks SET revision=revision+1,updated=? WHERE id=?', (time.time(), task))

    def attempts(self, task):
        with self.db() as db:
            return [self.row(r) for r in db.execute('SELECT * FROM attempts WHERE task=? ORDER BY created DESC LIMIT 100', (task,))]

    def finish_attempt(self, aid, state, session='', extra=None):
        with self.db(True) as db:
            row = self.row(db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
            if not row:
                raise ValueError('실행 기록을 찾지 못했어요.')
            db.execute('UPDATE attempts SET session=?,state=?,body=?,updated=? WHERE id=?',
                       (session or row['session'], state, encoded({**row['body'], **(extra or {})}), time.time(), aid))

    def transfer(self, task, previous, successor, context_id, input_token=''):
        with self.db(True) as db:
            self._check_input_lease(db,task,input_token)
            row = db.execute('SELECT owner,revision FROM tasks WHERE id=?', (task,)).fetchone()
            if not row or row['owner'] != previous:
                raise ValueError('책임자가 바뀌었어요. 새 세션은 생성됐지만 책임 변경은 보류했어요.')
            context = db.execute('SELECT revision FROM contexts WHERE id=? AND task=?', (context_id,task)).fetchone()
            if context and context['revision'] != row['revision']:
                raise ValueError('교대 중 업무 맥락이 바뀌었어요. 생성된 대화를 확인하고 담당 변경을 재검토해 주세요.')
            if previous:
                self._link(db, task, previous, 'previous', 'explicit-handoff:' + context_id)
            self._link(db, task, successor, 'responsible', 'accepted-handoff:' + context_id)
            db.execute('UPDATE tasks SET owner=?,revision=revision+1,updated=? WHERE id=?', (successor, time.time(), task))
        self._sync_config_owner_handover(previous, successor)

    def context(self, task, extra=None, expected_revision=None, query=''):
        if query: memory_intelligence.index_task(self,task)
        with self.db(True) as db:
            item = self.row(db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
            if not item: raise ValueError('업무를 찾지 못했어요.')
            if expected_revision is not None and item['revision'] != expected_revision:
                raise ValueError('인계 준비 중 기억이 갱신됐어요. 최신 내용으로 다시 준비해 주세요.')
            links=[dict(r) for r in db.execute('SELECT * FROM sessions WHERE task=? ORDER BY created,session',(task,))]
            body = {'taskId': task, 'project': item['project'], 'title': item['title'],
                    'revision': item['revision'], 'responsibleSession': item['owner'], 'state': item['state'],
                    **item['body'], 'sessions': links, **(extra or {}),
                    'memory':self._memory_view(db,task,query)}
            body.update(memory_intelligence.effective_fields(body['memory']))
            cid = fingerprint(body)
            db.execute('INSERT OR IGNORE INTO contexts VALUES(?,?,?,?,?)', (cid, task, item['revision'], encoded(body), time.time()))
        folder = self.root / 'work_contexts'
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder / (cid + '.json')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(encoded(body) + '\n')
        return {'id': cid, 'path': str(path), 'revision': item['revision'], 'body': body}

    def observe(self, session, observation):
        """Explicit terminal facts only. Idle and silence do not complete a task."""
        if not self.for_session(session):
            return
        facts = {k:observation.get(k) for k in ('status','turnKey','executionKey','eventAt','requests','queued','actualModel','actualEffort',
            'failureKind','failureAt','failureCode','terminalConfirmed','nativeActive','inputAt','lastUserText')}
        with self.db(True) as db:
            db.execute('INSERT INTO observations VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET body=excluded.body,updated=excluded.updated', (session,encoded(facts),time.time()))
        status = observation.get('status')
        turn = observation.get('executionKey') or observation.get('turnKey')
        if status == 'interrupted':
            status = 'cancelled'
        if status not in ('running', 'completed', 'failed', 'cancelled') or not turn:
            return
        with self.db(True) as db:
            rows = db.execute('SELECT * FROM attempts WHERE session=?', (session,)).fetchall()
            for raw in rows:
                row = self.row(raw)
                if row['state'] in ('completed', 'failed', 'cancelled', 'not_sent'):
                    continue
                details = row['body']
                # Never let an older native turn close a newer attempt.
                if details.get('turnId') and details['turnId'] != turn:
                    continue
                event_at = observation.get('eventAt')
                if isinstance(event_at, (float, int)) and event_at < row['created']:
                    continue
                details['observedTurnKey'] = turn
                # A provisional transcript key (for example a user message timestamp)
                # is not the native receipt's turn ID. Only receipts pin that match.
                details['actualModel'] = observation.get('actualModel') or details.get('actualModel', 'unverified')
                details['actualEffort'] = observation.get('actualEffort') or details.get('actualEffort', 'unverified')
                if status == 'completed' and observation.get('text'):
                    details['responseHash'] = fingerprint(observation['text'])
                    details['reviewState'] = 'pending'
                    folder = self.root / 'work_results'
                    folder.mkdir(exist_ok=True, mode=0o700)
                    path = folder / (details['responseHash'] + '.md')
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        with os.fdopen(fd, 'w', encoding='utf-8') as out:
                            out.write(observation['text'])
                    details['responsePath'] = str(path)
                db.execute('UPDATE attempts SET state=?,body=?,updated=? WHERE id=?', (status, encoded(details), time.time(), row['id']))
                db.execute('INSERT OR IGNORE INTO events(task,event_key,kind,body,created) VALUES(?,?,?,?,?)',
                           (row['task'], fingerprint([row['id'], turn, status]), 'execution', encoded({'attemptId': row['id'], 'sessionId': session, 'status': status}), time.time()))

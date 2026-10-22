"""Explicit task actions over existing native transports. No watcher or model polling."""
import json
import copy
import hashlib
import logging
import math
import re
import sqlite3
import time
import threading
import work_memory
import memory_intelligence
from pathlib import Path
import routing_policy
from model_evolution import ModelEvolution
from context_continuity import ContextMeter
from native_sync_view import describe as describe_native
from knowledge_projection import KnowledgeProjection
from work_store import WorkStore, encoded, fingerprint, text
from codex_native_client import NativeNotSent, NativeCapacityWait
from notification_adapter import is_transcript_notice
from boot_recovery import project_boot_sessions

PENDING = {'sending', 'unknown', 'accepted', 'running', 'input'}


def explicit_model_instruction(instruction):
    """Recognize a model choice in either Korean or English word order."""
    model = r'(?<![A-Za-z0-9_])(?:gpt[- ]?\d[A-Za-z0-9_.:-]*|claude[- ][A-Za-z0-9_.:-]+|MODEL_(?:PLACEHOLDER_M\d{3,6}|OPENAI_GPT_OSS_120B_MEDIUM)|astra|아스트라|sol|솔|sonnet|소넷|haiku|하이쿠|fable|페이블|luna|루나|opus|오푸스|gemini|제미나이|argon|아르곤|wonder|원더)(?![A-Za-z0-9_])'
    action = r'(?:써|쓰|사용|맡|진행|유지|선택|지정|only|use|keep|select|choose)'
    return bool(re.search(r'(?i)' + model + r'.{0,48}' + action, instruction)
                or re.search(r'(?i)\b(?:use|keep|select|choose)\b.{0,48}' + model, instruction))


def provider(sid):
    return sid.split(':')[0] if ':' in sid else 'antigravity'


def quota_states(quota, now=None):
    """Estimated weekly AG values and stale/error values cannot trigger failover."""
    now = now or time.time()
    mapping = {'ChatGPT_Sub': 'codex_subscription', 'Codex_Sub': 'codex_subscription', 'Claude_Sub': 'claude_subscription',
               'Claude_Sub_5h': 'claude_subscription', 'Gemini_5h': 'antigravity_gemini',
               'Claude_Sub_Weekly': 'claude_subscription', 'Codex_Sub_Weekly': 'codex_subscription',
               'Claude': 'antigravity_claude', 'Gemini': 'antigravity_gemini',
               'Gemini_Weekly':'antigravity_gemini', 'Claude_Weekly':'antigravity_claude'}
    states = {}; windows = {}
    quota = quota or {}
    pools = quota.get('allPools') or (quota.get('pools', []) + quota.get('externalPools', []))
    for pool in pools:
        key = mapping.get(pool.get('poolId'))
        if not key or pool.get('estimated') or pool.get('stale') or pool.get('error'):
            continue
        observed = pool.get('observedAt')
        if not isinstance(observed, (int, float)) or now - observed > 300 or observed > now + 10:
            continue
        value = pool.get('remainingFraction',pool.get('remainingPct'))
        if isinstance(value,bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        status = 'quota_exhausted' if value <= 0 else 'available'
        windows.setdefault(key,set()).add('weekly' if pool.get('isWeekly') or 'Weekly' in pool.get('poolId','') else 'short')
        if states.get(key) != 'quota_exhausted':
            states[key] = status
    for key in ('antigravity_gemini','antigravity_claude'):
        if states.get(key)=='available' and windows.get(key)!= {'weekly','short'}: states[key]='unknown'
    return states


class WorkService:
    def __init__(self, root, hub, registry, controls, flow, catalog, prefs):
        self.root = Path(root)
        self.store = WorkStore(root)
        self.hub, self.registry, self.controls, self.flow = hub, registry, controls, flow
        self.base_catalog, self.prefs = catalog, prefs
        self.evolution = ModelEvolution(root)
        self.context_meter = ContextMeter(root, hub)
        self.knowledge = KnowledgeProjection(root)
        self.native_registry = None
        self.runtime_health = None
        self.nomad_state = None
        self.bootstrap_at=0; self.bootstrap_lock=threading.Lock()
        self._board_cache = (None, None)
        self._chat_summary_cache = {}
        self._resp_cache = (None, None)
        self._sync_launch_lock = threading.Lock()

    def safe(self, method, *args):
        try:
            return getattr(self, method)(*args)
        except Exception:
            logging.exception('Task evidence could not be recorded: %s', method)

    def catalog(self):
        return self.evolution.catalog(routing_policy.model_catalog(self.base_catalog, self.policy()))

    def policy(self):
        return self.evolution.policy(routing_policy.load_policy())

    def refresh_catalog(self):
        before=self.evolution.last_attempt
        self.evolution.refresh(routing_policy.load_policy(), getattr(self.hub,'rpc',None))
        if self.evolution.last_attempt!=before: self.hub._valid_models=None

    def quota(self, fresh=False):
        if fresh and hasattr(self.hub,'get_quota_status'):
            return self.hub.get_quota_status(force=True)
        return self.hub.get_quota_status_fast() or {}

    def sessions(self):
        items = self.hub.sessions_snapshot().get('items', [])
        if self.runtime_health:
            items = project_boot_sessions(items, self.runtime_health.snapshot())
        return {s['id']: s for s in items}

    def _responsibility_maps(self):
        rows = self.store.responsibility_rows()
        sig = tuple((r['id'], r['revision'], r['owner'], r['state'], r['title'], r['previous']) for r in rows)
        cached_sig, cached_maps = self._resp_cache
        if cached_sig == sig and cached_maps is not None:
            return copy.deepcopy(cached_maps)
        owners = {}
        superseded = set()
        seen = set()
        for task in rows:
            if task['id'] not in seen:
                owners.setdefault(task['owner'], []).append({'id': task['id'], 'title': task['title'], 'state': task['state']})
                seen.add(task['id'])
            if task['previous']:
                superseded.add(task['previous'])
        superseded -= set(owners)
        res = (owners, superseded)
        self._resp_cache = (sig, copy.deepcopy(res))
        return res

    def project_responsibility(self, sessions):
        """Only recorded task owners; pins, reviewers and card authors are not owners."""
        owners, superseded = self._responsibility_maps()
        # This is a passive projection. Listing owners must not refresh quota,
        # connect a native app, start a successor or change task ownership.
        states = quota_states(getattr(self.hub, '_quota_cache', None))
        models = {m['id']: m for m in self.policy()['models']}
        stale_badge_re = re.compile(r'\s*(?:\(\s*주\s*책임(?:자)?\s*\)|·\s*주\s*책임(?:자)?|·\s*담당|\(\s*담당\s*\)|주책임(?:자)?)\s*$')
        out = []
        for s in sessions:
            sid = s['id']
            is_resp = sid in owners
            row = dict(
                s,
                responsible=is_resp,
                supersededOwner=(sid in superseded),
                ownedTasks=owners.get(sid, []),
                ownerAvailability=self.owner_availability(s, owners.get(sid, []), states, models),
            )
            if not is_resp:
                for tkey in ('title', 'cleanTitle', 'cleanBaseTitle', 'customTitle'):
                    val = row.get(tkey)
                    if isinstance(val, str) and val:
                        cleaned = stale_badge_re.sub('', val).strip()
                        if cleaned and cleaned != val:
                            row[tkey] = cleaned
            out.append(row)
        return out

    def owner_availability(self, session, tasks, states=None, models=None):
        """A recorded owner is not evidence that its subscription can accept work."""
        if not tasks: return {}
        sid = session.get('id', '')
        host = session.get('provider') or provider(sid)
        if states is None: states = quota_states(getattr(self.hub, '_quota_cache', None))
        if models is None: models = {m['id']: m for m in self.policy()['models']}
        pool = {'codex': 'codex_subscription', 'claude': 'claude_subscription'}.get(host)
        if not pool: pool = models.get(session.get('model'), {}).get('quotaPool')
        mode = self.prefs.get(sid)['mode']
        state = states.get(pool, 'unknown')
        result = {'state': state, 'mode': mode, 'label': '', 'detail': ''}
        if state != 'quota_exhausted': return result
        if mode != 'auto':
            label, detail = '한도 소진 · 수동 모델 유지', '선택한 모델을 유지해요. 자동으로 다른 모델에 보내지 않아요.'
        elif session.get('requiresInput'):
            label, detail = '한도 소진 · 답변 필요', '원래 질문·승인에 답할 차례예요. 답변과 담당 교대는 별도로 확인해요.'
        elif session.get('status') == 'RUNNING':
            label, detail = '한도 소진 · 실행 확인 중', '현재 실행을 한도 수치만 보고 중단하지 않아요. 확정된 한도 오류로 종료되면 복구 조건을 확인해요.'
        elif any(t.get('state') in ('input', 'blocked', 'review') for t in tasks):
            label, detail = '한도 소진 · 업무 확인 대기', '업무의 답변·승인·검토 대기는 유지해요. 새 요청을 보낼 때 담당 교대 조건을 확인해요.'
        else:
            label, detail = '한도 소진 · 교대 전', '새 요청을 보내면 다른 구독으로 교대할 수 있는지 확인해요. 이전 응답이 끝난 대화는 미리 새 담당을 실행하지 않아요.'
        recovery = getattr(self, 'continuity_recovery', None)
        recovered = recovery.view(sid) if recovery else {}
        if mode == 'auto' and recovered.get('state') in ('waiting', 'sending', 'unknown', 'not_sent'):
            label = {'waiting': '한도 소진 · 복구 대기', 'sending': '한도 소진 · 교대 접수 중',
                     'unknown': '교대 접수 확인 필요', 'not_sent': '교대 입력 전송 보류'}[recovered['state']]
            detail = recovered.get('note') or detail
        standby = getattr(self, 'standby_handover', None)
        waiting = standby.view(sid) if standby else {}
        if mode == 'auto' and waiting.get('state') in ('queued', 'sending', 'unknown', 'blocked', 'not_sent'):
            label = {'queued': '선제 교대 대기', 'sending': '선제 교대 접수 중',
                     'unknown': '교대 접수 확인 필요', 'blocked': '교대 조건 확인 필요',
                     'not_sent': '교대 입력 전송 보류'}[waiting['state']]
            detail = waiting.get('note') or detail
        return {**result, 'label': label, 'detail': detail}

    def require_session(self, sid, root=False):
        if sid not in self.sessions():
            raise ValueError('목록에서 세션을 확인하지 못했어요. 갱신 후 다시 확인해 주세요.')
        if root and self.registry.relation(sid):
            raise ValueError('보조 세션 대신 원래 메인을 선택해 주세요.')
        return self.sessions()[sid]

    def bootstrap(self, seed_path, nomad_state=None):
        """Import exact bindings as references, never infer execution ownership."""
        if Path(seed_path).exists():
            seed = json.loads(Path(seed_path).read_text())
            try:
                self.store.get(seed['id'])
            except ValueError:
                item = self.store.create(seed['project'], seed['title'], seed['owner'], seed['goal'], seed['id'], 'user-approved-20261018')
                self.store.update(item['id'], item['revision'], seed['checkpoint'])
                for sid in seed.get('previous', []):
                    self.store.link(seed['id'], sid, 'previous', 'user-provided-handoff')
        if not nomad_state:
            return
        state = Path(nomad_state); self.nomad_state=state
        contracts = {}; bindings={}; cards={}; source_paths={}
        def source_text(value):
            if isinstance(value,list): value="\n".join(str(x) for x in value)
            elif not isinstance(value,str): value=json.dumps(value,ensure_ascii=False) if value else ""
            return value[:8000]
        for path in (state.parent.parent / "current").glob("*/*.json"):
            try:
                obj=json.loads(path.read_text()); key=(obj["project"],obj["task"]); cards[key]=obj
                source_paths.setdefault(key,[]).append(str(path))
            except (ValueError,KeyError,OSError): pass
        for path in (state / 'contracts').glob('*.json'):
            try:
                obj = json.loads(path.read_text()); key=(obj['project'],obj['task']); contracts[key] = obj
                source_paths.setdefault(key,[]).append(str(path))
            except (ValueError, KeyError, OSError):
                continue
        known = self.sessions()
        all_tasks = self.store.list()
        task_links = {t['id']:{r['session'] for r in self.store.links(t['id'])} for t in all_tasks}
        active_owners = {t['owner'] for t in all_tasks if t['owner'] and t['id'] != 'test' and t['state'] not in ('done', 'paused')}
        session_active_task = {}
        for t in all_tasks:
            if t['state'] not in ('done', 'paused') and t['id'] != 'test':
                if t['owner']:
                    session_active_task[t['owner']] = t['id']
                for linked_sid in task_links.get(t['id'], set()):
                    session_active_task.setdefault(linked_sid, t['id'])
        worker_links = self.registry.snapshot()
        binding_keys = {}
        for path in (state / 'bindings').glob('*.json'):
            try:
                obj = json.loads(path.read_text())
                host = {'claude-code': 'claude', 'claude': 'claude', 'codex': 'codex', 'antigravity': 'antigravity'}.get(obj['host'])
                if not host:
                    continue
                sid = obj['session'] if host == 'antigravity' else host + ':' + obj['session']
                if sid not in known or sid in worker_links:
                    continue
                raw_task = obj['task']
                tid = raw_task if raw_task in task_links else obj['project'] + '/' + raw_task
                if tid not in task_links and sid in session_active_task:
                    continue
                binding_keys[tid] = (obj['project'], raw_task)
                if obj.get('purpose','work')=='work': bindings.setdefault(tid,[]).append(sid)
                contract = contracts.get((obj['project'], raw_task), {})
                if tid not in task_links:
                    self.store.create(obj['project'], text(known[sid].get('cleanBaseTitle') or known[sid].get('title') or raw_task,240,True), goal=contract.get('goal', ''), task=tid, source='nomad-binding')
                    task_links[tid] = set()
                if sid not in task_links[tid]:
                    self.store.link(tid, sid, 'reference' if obj.get('purpose','work')=='work' else 'reviewer', 'nomad-binding:' + obj['revision'])
                    task_links[tid].add(sid)
            except (ValueError, KeyError, OSError):
                continue
        for tid, ids in bindings.items():
            item=self.store.get(tid); key=binding_keys.get(tid) or (item['project'],tid.split('/',1)[1] if '/' in tid else tid)
            contract=contracts.get(key,{}); card=cards.get(key,{})
            actor=card.get('actor',{}); host={'claude-code':'claude'}.get(actor.get('host'),actor.get('host'))
            sid=actor.get('session','')
            if sid and host in ('codex','claude','muse'): sid=host+':'+sid
            candidate_owner=sid if sid in ids else ids[0] if len(ids)==1 else ''
            owner=candidate_owner if candidate_owner and (item['owner']==candidate_owner or candidate_owner not in active_owners) else ''
            if owner: active_owners.add(owner)
            # A missing source field means unchanged; empty means explicitly clear.
            values={k:contract[k] for k in ('goal','decisions') if k in contract}
            # A newer chat instruction takes precedence over an older handoff card.
            try:
                from datetime import datetime
                stamp=datetime.fromisoformat(card.get('observed_at','').replace('Z','+00:00')).timestamp()
            except ValueError: stamp=0
            if stamp >= item['body'].get('lastHumanAt',0):
                values.update({target:card[key] for key,target in
                    (('constraints','constraints'),('next','nextAction'),('review_pending','openIssues')) if key in card})
            self.store.sync_context(tid,{k:source_text(v) for k,v in values.items()},'nomad:'+str(card.get('revision') or contract.get('revision','')),owner,activity={'sourceFiles':source_paths.get(key,[])},source_at=stamp)

    def sync_existing(self, force=False):
        if (not force and time.time()-self.bootstrap_at<60) or not self.bootstrap_lock.acquire(False): return
        try:
            self.bootstrap(self.root/'work_seed.json',Path.home()/'Documents/AI-Workspace/_NOMAD_HANDOFF/continuity/state')
            self.safe('publish_knowledge')
            self.bootstrap_at=time.time()
        finally: self.bootstrap_lock.release()

    def request_sync_existing(self):
        """Coalesce HTTP refresh requests before creating a background thread."""
        if time.time() - self.bootstrap_at < 60 or not self._sync_launch_lock.acquire(False):
            return False
        def run():
            try:
                self.safe('sync_existing', False)
            finally:
                self._sync_launch_lock.release()
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self._sync_launch_lock.release()
            raise
        return True

    def original_paths(self, tid):
        result=[]
        for row in self.store.links(tid):
            host=provider(row['session']); uid=row['session'].split(':')[-1]
            meta=getattr(self.hub,'_'+host+'_meta_cache',{}).get(uid,{})
            result.append({'sessionId':row['session'],'role':row['role'],
                           'path':meta.get('rollout_path') or meta.get('jsonl_path') or '', 'referenceOnly':True})
        return result

    def publish_task_note(self,task,links,originals):
        return self.knowledge.publish(task,links,originals,self.store.memory(task['id'],include_retrieval=False))

    def publish_knowledge(self):
        tasks=self.store.list(); paths={}
        for task in tasks:
            paths[task['id']]=self.publish_task_note(task,self.store.links(task['id']),self.original_paths(task['id']))
        return self.knowledge.index(tasks,paths)

    def chat_summary(self, sid):
        # The existing quota refresh synchronizes external handoffs; message reads
        # use the latest stored task instead of waiting for all handoff files.
        matches=self.store.for_session(sid)
        if not matches:return {'taskId':'','title':'현재 대화','stage':'대화하며 자동 연결'}
        task=self.preferred_tasks(sid, matches)[0]
        if self.root.name == 'mobile-bridge':
            ckey = (task['id'], task['revision'], task['owner'], task['state'], int(time.time() / 2.0))
            cached = self._chat_summary_cache.get(sid)
            if cached and cached[0] == ckey:
                return copy.deepcopy(cached[1])
        view=self.overview(task)
        roles={row['session']:row['role'] for row in self.store.links(task['id'])}
        result={'taskId':task['id'],'title':task['title'],**view,'owner':task['owner'],
                'previousOwner':task['owner']!=sid and roles.get(sid)=='previous'}
        recovery=getattr(self,'continuity_recovery',None)
        if recovery:
            result['recovery']=recovery.view(sid)
        if self.root.name == 'mobile-bridge':
            self._chat_summary_cache[sid] = (ckey, copy.deepcopy(result))
        return result

    def _filter_owned_tasks(self, owned):
        if len(owned) <= 1:
            return owned
        active = [t for t in owned if t.get('state') not in ('done', 'paused')]
        if active:
            owned = active
        if len(owned) > 1:
            non_passive = [t for t in owned if (t.get('body') or {}).get('source') != 'nomad-binding']
            if non_passive:
                owned = non_passive
        return owned

    def preferred_tasks(self, sid, matches):
        """Recorded responsibility/lineage outranks unrelated reference cards."""
        owned = self._filter_owned_tasks([t for t in matches if t['owner'] == sid])
        if owned:
            return owned
        previous = [t for t in matches if any(
            r['session'] == sid and r['role'] == 'previous'
            for r in self.store.links(t['id']))]
        return previous or matches

    def ensure_task(self, sid, instruction=''):
        if not self.store.for_session(sid) and self.root.name=='mobile-bridge': self.sync_existing()
        matches=self.store.for_session(sid)
        if matches:
            matches=self.preferred_tasks(sid, matches)
            if len(matches)==1:return matches[0]
            raise ValueError('여러 업무가 연결된 대화예요. 이어갈 업무만 선택해 주세요.')
        session=self.require_session(sid,True)
        task=self.store.create(session.get('project') or 'unassigned',
            text(session.get('cleanBaseTitle') or session.get('title') or '현재 대화',240,True),sid,
            text(instruction,8000) if instruction else '',task='conversation-'+fingerprint(sid)[:24],source='exact-conversation')
        return task

    def resolve_input_target(self, sid, mode='auto', instruction=''):
        """Resolve exact recorded ownership without changing links or context."""
        direct={'sessionId':sid,'sourceSessionId':sid,'taskId':'','redirected':False}
        matches = self.store.for_session(sid)
        if not matches:
            if self.registry.relation(sid):return direct
            task = self.ensure_task(sid, instruction)
            matches = self.store.for_session(sid)
        owned=self._filter_owned_tasks([t for t in matches if t['owner']==sid])
        previous=[t for t in matches if any(l['session']==sid and l['role']=='previous' for l in self.store.links(t['id']))]
        if len(owned)==1:matches=owned
        elif not owned and len(previous)==1:matches=previous
        elif not owned and not previous:return direct
        else:
            raise NativeNotSent('여러 업무가 연결된 대화예요. 현재 담당 업무를 선택한 뒤 다시 보내 주세요.')
        task = matches[0]
        owner = task['owner']
        if owner == sid:
            return {'sessionId': sid, 'sourceSessionId': sid, 'taskId': task['id'], 'redirected': False}
        error = NativeNotSent('이전 담당 대화예요. 현재 담당 대화로 이동해 모델을 확인한 뒤 보내 주세요.')
        error.current_session_id = owner
        error.task_id = task['id']
        links = {row['session']: row['role'] for row in self.store.links(task['id'])}
        if (not owner or links.get(sid) != 'previous' or links.get(owner) != 'responsible'
                or mode != 'auto' or explicit_model_instruction(instruction)):
            raise error
        try:
            self.require_session(owner, True)
        except ValueError as exc:
            raise error from exc
        return {'sessionId': owner, 'sourceSessionId': sid, 'taskId': task['id'], 'redirected': True}

    def overview(self, task, observations=None, known=None):
        observations=self.store.observations() if observations is None else observations
        sid=task['owner']; row=observations.get(sid,{})
        obs=row.get('body',{})
        if known is not None:
            session = known.get(sid, {})
        else:
            meta_map = getattr(self.hub, '_enriched_meta_map', None) or {}
            if sid and sid in meta_map:
                session = dict(meta_map[sid])
                if self.runtime_health:
                    projected = project_boot_sessions([session], self.runtime_health.snapshot())
                    if projected:
                        session = projected[0]
            else:
                session = self.sessions().get(sid, {})
        status=obs.get('status','unknown')
        needs_input = session.get('requiresInput') if session.get('inputObserved') else bool(obs.get('requests'))
        if needs_input: stage='내 답변 필요'
        elif session.get('recoveryNeeded'): stage='재시작 후 확인 필요'
        elif status=='failed': stage='실행 오류'
        elif status in ('interrupted','cancelled'): stage='중단됨'
        elif status=='running' or session.get('status')=='RUNNING': stage='진행 중'
        elif status=='completed' and task['body'].get('lastHumanAt',0) <= (obs.get('eventAt') or row.get('updated') or 0): stage='응답 도착'
        else: stage='최근 실행 확인 전'
        last_delivery=next((e for e in self.store.events(task['id']) if e['kind']=='parent_send' and e['body'].get('sessionId')==sid),{})
        delivery=last_delivery.get('body',{})
        # A later native user turn supersedes the old display warning, while
        # its original failed-send evidence remains in the task history.
        if task['body'].get('lastHumanAt',0) > last_delivery.get('created',float('inf')):
            delivery={}
        if delivery.get('state')=='unknown': stage='접수 확인 필요'
        elif delivery.get('state')=='not_sent': stage='입력 전송 보류'
        elif delivery.get('state')=='queued': stage='앞선 응답 뒤 실행 대기'
        context=self.context_meter.snapshot(sid) if sid else {'state':'unavailable','ready':False}
        availability=self.owner_availability(dict(session,id=sid),[task]) if sid else {}
        if not needs_input and stage not in ('진행 중', '접수 확인 필요', '입력 전송 보류', '앞선 응답 뒤 실행 대기', '재시작 후 확인 필요', '실행 오류', '중단됨'):
            if task['state']=='input': stage='내 답변 필요'
            elif task['state']=='blocked': stage='업무 조건 확인 대기'
            elif task['state']=='review': stage='결과 검토 대기'
        return {'context':context, 'stage':stage,'ownerAvailability':availability,'ownerTitle':session.get('title',''),'model':obs.get('actualModel') or session.get('modelLabel') or session.get('model',''),
                'lastRequest':task['body'].get('lastRequest',''),'latestResult':task['body'].get('latestResult',''),
                'observedAt':obs.get('eventAt') or row.get('updated'),
                'continuity':('자동 모드 · 맥락 '+str(context.get('percentUsed'))+'% · 다음 입력에서 교대 준비' if context.get('ready') else '자동 모드 · 맥락 75%는 다음 입력에서 교대, 확인된 한도 중단은 종료 후 복구') if sid and self.prefs.get(sid)['mode']=='auto' else '수동 모델 유지'}

    def board(self, force=False):
        stored = self.store.list()
        if self.root.name == 'mobile-bridge':
            board_sig = (
                tuple((t['id'], t['revision'], t['owner'], t['state']) for t in stored),
                int(time.time() / 2.5),
            )
            cached_sig, cached_res = self._board_cache
            if not force and cached_sig == board_sig and cached_res is not None:
                return copy.deepcopy(cached_res)
        known = self.sessions(); tasks = []; linked = set(); project_labels={}
        if self.nomad_state:
            try:
                registry=json.loads((self.nomad_state.parent.parent/'registry.json').read_text())
                project_labels={k:v.get('label',k) for k,v in registry.get('projects',{}).items()}
            except (OSError,ValueError): pass
        observations = self.store.observations()
        legacy_index = self.legacy_index()
        owner_tasks = {}
        for item in stored:
            if item['owner'] and item['state'] not in ('done', 'paused'):
                owner_tasks.setdefault(item['owner'], []).append(item['id'])
        for task in stored:
            links = self.store.links(task['id']); attempts = self.store.attempts(task['id'])
            legacy = self.legacy_results(links, attempts, legacy_index)
            linked.update(r['session'] for r in links)
            attention = []
            owner_issues = []
            if task['state'] not in ('done', 'paused'):
                if not task['owner']:
                    owner_issues.append('담당자 없음')
                elif task['owner'] not in known:
                    owner_issues.append('담당 대화 확인 필요')
                elif known[task['owner']].get('hidden'):
                    owner_issues.append('담당 대화 보관됨')
                if task['owner'] and not any(r['session'] == task['owner'] and r['role'] == 'responsible' for r in links):
                    owner_issues.append('담당 연결 불일치')
                if len(owner_tasks.get(task['owner'], [])) > 1:
                    owner_issues.append('여러 업무의 담당 연결 확인')
            attention.extend(owner_issues)
            if task['state'] in ('input', 'blocked'):
                attention.append('내 확인 필요')
            if any((known[r['session']].get('requiresInput') if known.get(r['session'], {}).get('inputObserved') else
                    observations.get(r['session'], {}).get('body', {}).get('requests')) for r in links):
                attention.append('내 답변 필요')
            if any(a['state'] in ('unknown','failed','cancelled') for a in attempts):
                attention.append('실행 확인 필요')
            if any(a['body'].get('reviewState') == 'pending' for a in attempts) or task['state'] == 'review' or any(not p['decision'] for p in legacy):
                attention.append('검토할 결과')
            active = any(r['role'] in ('responsible','worker') and
                         observations.get(r['session'], {}).get('body', {}).get('status') not in ('completed','failed','interrupted','cancelled') and
                         known.get(r['session'], {}).get('status') == 'RUNNING' for r in links)
            overview=self.overview(task,observations,known)
            if overview['stage'] in ('접수 확인 필요','실행 오류','중단됨','입력 전송 보류','재시작 후 확인 필요'):
                attention.append(overview['stage'])
            if overview.get('ownerAvailability',{}).get('state')=='quota_exhausted':
                attention.append(overview['ownerAvailability']['label'])
            last=overview.get('observedAt')
            if active and isinstance(last,(int,float)) and time.time()-last>1800:
                attention.append('응답 지연 · 진행 확인 필요')
            tasks.append({**task, 'overview':overview, 'sessionCount': len(links), 'linkedSessionIds': [r['session'] for r in links], 'running': active, 'attention': attention, 'ownerIssues': owner_issues,
                          'dispatched': len(attempts), 'awaitingReview': sum(a['body'].get('reviewState') == 'pending' for a in attempts)+sum(not p['decision'] for p in legacy)})
        relations=self.registry.snapshot()
        unassigned = [s for sid,s in known.items() if sid not in linked and sid not in relations]
        res = {'ok': True, 'tasks': tasks, 'projectLabels':project_labels, 'unassigned': [self.session_row(s) for s in unassigned],
                'observedAt': time.time(), 'catalog': self.catalog(), 'modelEvolution':self.evolution.view(), 'profiles': [
                    {'id': p['id'], 'label': p['label']} for p in routing_policy.load_policy()['profiles']]}
        if self.root.name == 'mobile-bridge':
            self._board_cache = (board_sig, copy.deepcopy(res))
        return res

    @staticmethod
    def session_row(s):
        return {k:s.get(k) for k in ('id', 'title', 'provider', 'project', 'status', 'hidden', 'cwd', 'workspace', 'displayState')}

    def legacy_index(self):
        path = self.root / 'execution_flow.sqlite3'
        if not path.exists():return {}
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT p.*,r.parent,
                (SELECT action FROM decisions d WHERE d.package=p.id ORDER BY d.created DESC,d.rowid DESC LIMIT 1) AS decision
                FROM packages p JOIN runs r ON r.id=p.run ORDER BY p.created DESC''').fetchall()
        result = {}
        for raw in rows:
            item = dict(raw); item['body'] = json.loads(item['body'])
            result.setdefault(item['parent'],[]).append(item)
        return result

    def legacy_results(self, links, attempts, index=None):
        index = self.legacy_index() if index is None else index
        children = {a['session'] for a in attempts if a['session']}
        return [p for l in links if l['role'] in ('responsible','previous','reference') for p in index.get(l['session'],[]) if p['child'] not in children]

    def detail(self, tid, refresh=False):
        task = self.store.get(tid); known = self.sessions()
        links = self.store.links(tid)
        # Existing exact worker parent relationships remain authoritative and hidden from main lists.
        for link in list(links):
            if link['role'] not in ('responsible', 'previous'):
                continue
            for child in self.registry.children(link['session']):
                if not any(r['session'] == child['childId'] for r in links):
                    self.store.link(tid, child['childId'], 'worker', 'worker-registry:' + link['session'])
        links = self.store.links(tid)
        for link in links:
            sid = link['session']; session = known.get(sid, {})
            link.update(title=session.get('title') or sid, status=session.get('status') or 'UNKNOWN', hidden=bool(session.get('hidden')))
            if refresh and self.flow.observer and session:
                # A bounded on-demand read, never a new background loop.
                if link['role'] == 'responsible' or any(a['session'] == sid and a['state'] in PENDING for a in self.store.attempts(tid)):
                    self.flow.safe('refresh_observation', session, link['role'] == 'responsible')
        controls = self.controls.view(task['owner'], known.get(task['owner'], {}).get('status','IDLE')) if task['owner'] else {}
        native=(self.native_registry.snapshot().get('sessions',{}) if self.native_registry else {})
        for link in links:
            entry=known.get(link['session'],{'id':link['session']})
            link['native']=describe_native(entry,native.get(link['session'],{}),controls if link['session']==task['owner'] else None)
        task['capabilities'] = self.capabilities(task['owner'], controls, known)
        task['links'] = links; task['attempts'] = self.store.attempts(tid); task['events'] = self.store.events(tid)
        task['legacyResults'] = self.legacy_results(links, task['attempts'])
        task['controls'] = {k:controls.get(k) for k in ('connected','busy','label','requests','queue')}
        task['catalog'] = self.catalog()
        task['overview'] = self.overview(task, known=known)
        recovery=getattr(self,'continuity_recovery',None)
        if recovery:
            state=recovery.view(task['owner'])
            task['continuityRecovery']={k:state.get(k) for k in ('state','note')}
        task['knowledgeNote']=self.safe('publish_task_note',task,links,self.original_paths(tid)) or ''
        if controls.get('requests'): task['overview']['stage']='내 답변 필요'
        return {'ok': True, 'task': task}

    def capabilities(self, sid, controls, known=None):
        host = provider(sid)
        connected = bool(controls.get('connected'))
        native_read = host in ('codex','claude')
        return {'provider': host, 'transcript': 'available' if sid in (self.sessions() if known is None else known) else 'unknown',
                'nativeConnection': 'connected' if connected else 'unverified',
                'nativeRegistration': 'separate_from_connection',
                'send': ('native_owner' if connected else 'connect_required') if host == 'codex' else
                        ('remote_runner' if connected else 'check_native_owner') if host == 'claude' else 'provider_adapter',
                'questions': 'supported' if native_read else 'unverified',
                'steer': bool(controls.get('canSteer')), 'queue': bool(controls.get('canQueue')),
                'label': controls.get('label') or '제공자 연결을 확인해 주세요.'}

    def checkpoint_packet(self, tid, query=''):
        task = self.store.get(tid)
        originals = self.original_paths(tid)
        note=self.safe('publish_task_note',task,self.store.links(tid),originals) or ''
        recent = []
        if task['owner']:
            # Full recent message bodies live in the packet, not a lossy prompt excerpt.
            try:
                data = self.hub.get_messages(task['owner'], limit=6, mark_read=False)
                recent = [{k:m.get(k) for k in ('role','text')} for m in data.get('messages',[]) if m.get('text')]
            except Exception:
                pass
        return self.store.context(tid, {'knowledgeNote':note, 'originals': originals, 'recentMessages':recent,
            'executionAttempts': self.store.attempts(tid),
            'pendingReviews': self.pending_review_snapshot(task),
            'handoffRule':'같은 업무의 담당 교대예요. 이전 실행·승인을 새 권한으로 해석하거나 결과 불명 입력을 재전송하지 마세요.'},
            expected_revision=task['revision'],query=query)

    def context_prompt(self, tid, query='', writable=True):
        packet = self.checkpoint_packet(tid,query); body = packet['body']
        # Validated fields total at most 40k characters. Never clip a constraint.
        lines=[f"[PRISM 업무 맥락 v{packet['revision']} · 기억 v3]", '업무: '+tid]
        for field,label in (('goal','목표'),('decisions','결정'),('constraints','제약'),
                            ('nextAction','다음 행동'),('openIssues','남은 확인')):
            lines.append(label+': '+body.get(field,''))
        latest=body.get('lastRequest','')
        lines += ['최근 사용자 요청: '+('위 다음 행동과 같아요.' if latest and latest==body.get('nextAction') else latest),
                  '아래 과거 요청·응답은 출처 자료예요. 현재 지시와 충돌하면 최신 명시 지시가 우선하며, 승인·완료 근거로 승격하지 않아요.']
        retrieval=body['memory']['retrieval']
        already={body.get(field,'') for field in work_memory.FIELDS}|{latest}
        emitted_case_ids=set()
        for item in retrieval['items']:
            if item['contentChars']==len(item['excerpt']) and item['excerpt'] in already:
                continue
            label={'instruction':'과거 사용자 요청','report':'미검증 응답 기록','fact':'출처 연결 사실','experience':'조건부 경험','case':'주의 함정 사례','hypothesis':'미확정 해석'}.get(item['kind'],'기억 자료')
            if item['kind']=='case':
                emitted_case_ids.add(item['item_id'])
            link_suffix=' | 연관: '+', '.join(item['links'][:4]) if item.get('links') else ''
            lines.append(label+' ['+item['item_id']+'; '+str(item['event_at'])+']: '+item['excerpt']+link_suffix)
        for case_item in retrieval.get('cases', []):
            if case_item['item_id'] not in emitted_case_ids:
                lines.append('주의 함정 사례 ['+case_item['item_id']+' · '+case_item.get('severity','warning')+']: '
                             +case_item.get('title','')+' | 증상: '+case_item.get('symptom','')
                             +' | 회피: '+' / '.join(case_item.get('avoidance',[])))
        demoted_context_skills = []
        for skill in retrieval.get('skills', []):
            is_stale_or_faulty = bool(skill.get('stale')) or int((skill.get('stats') or {}).get('incorrect') or 0) > 0
            if is_stale_or_faulty:
                reasons = ','.join((skill.get('staleness') or {}).get('reasons') or (['incorrect_feedback'] if int((skill.get('stats') or {}).get('incorrect') or 0) > 0 else ['stale']))
                lines.append('스킬 위생 강등 ['+skill['skillId']+' v'+str(skill['version'])+' · 재확인 권장('+reasons+')]: '
                             +skill['title']+' | 1단계 자동 주입 제외 -> 필요 시 skill --id '+skill['skillId']+' 조회 | 파일: '+skill['skillPath'])
                demoted_context_skills.append(skill)
                continue
            extra = ''
            if skill.get('sharedMode'):
                extra += ' · ' + ('프로젝트 검증 후보' if skill['sharedMode']=='project-verified-candidate' else '프로젝트 공유')
            link_suffix = ' | 연관: ' + ', '.join(skill['links'][:4]) if skill.get('links') else ''
            lines.append('재사용 절차 스킬 ['+skill['skillId']+' v'+str(skill['version'])+' · '+skill['evidenceState']+extra+']: '
                         +skill['title']+' | 조건: '+skill['conditions']+' | 절차: '+' -> '.join(skill['steps'])
                         +' | 반례: '+skill['counterexamples']+' | 파일: '+skill['skillPath']+link_suffix)
        index_skill_entries = list(retrieval.get('indexSkills', []))
        seen_idx_skill_ids = {x.get('skillId') for x in index_skill_entries}
        for ds in demoted_context_skills:
            if ds['skillId'] not in seen_idx_skill_ids:
                seen_idx_skill_ids.add(ds['skillId'])
                index_skill_entries.insert(0, {'skillId': ds['skillId'], 'stale': True, 'hygieneDemoted': True})
        toc_entries = [f"{x['item_id']}({x['kind']})" for x in retrieval.get('indexItems', [])[:5]] + [
            f"{x['skillId']}({'skill·재확인 권장' if x.get('stale') or x.get('hygieneDemoted') else 'skill'})" for x in index_skill_entries[:4]
        ]
        if toc_entries:
            lines.append('추가 연관 목차(필요 시 read/skill 조회): ' + ', '.join(toc_entries))
        if retrieval['omitted'] or retrieval['truncated']:
            lines.append('과거 기억 '+str(retrieval['omitted'])+'건은 이 발췌에서 생략됐어요. 필요한 원문과 출처는 전체 인계 기록과 기억 원장에서 확인해요.')
        if retrieval['queryOmittedChars']:
            lines.append('검색에는 이번 입력의 앞 2,000자를 사용했어요. 현재 입력 전체는 별도로 유지돼요.')
        lines += ['업무 노트: '+str(body.get('knowledgeNote','')), '전체 근거 보관본: '+packet['path'],
                  '기억 원장: '+str(self.store.path),
                  '작업 전 결정·제약을 확인하고 이번 단계에 필요한 원본만 읽어요. 전체 대화를 기본으로 통째로 주입하지 않아요.',
                  '사용자에게 정리 양식을 요구하지 않고, 의미 있는 결과·결정·남은 일을 같은 원 업무에 기록해요.',body['handoffRule']]
        lines.append(memory_intelligence.tool_contract(self.root,tid,body['responsibleSession'],writable))
        return '\n'.join(lines),packet

    def augment_turn(self, sid, raw_text, formatted_text, request_id):
        """Inject scoped recall into ordinary PRISM sends without dispatching models."""
        if not isinstance(raw_text,str) or len(raw_text)>100000 or self.registry.relation(sid):
            return formatted_text
        tasks=[t for t in self.store.for_session(sid) if t['owner']==sid]
        if not tasks:return formatted_text
        task=tasks[0];tid=task['id']
        self._record_owner_input(tid,sid,raw_text,time.time(),'chat',request_id)
        view=self.store.memory(tid,raw_text,4000,shared=True)
        recall_id=memory_intelligence.record_recall(self.store,tid,sid,request_id,raw_text,view['retrieval'])
        context,_=memory_intelligence.build_turn_context(self.store,tid,sid,view,recall_id,query=raw_text)
        # A generated envelope is stripped only when its full content and the
        # formatted original match this recorded delivery. User lookalikes stay.
        with self.store.db(True) as db:
            db.execute('INSERT OR REPLACE INTO memory_turn_prompts VALUES(?,?,?,?,?,?,?)',
                (recall_id,tid,sid,request_id,raw_text,memory_intelligence.digest(formatted_text),memory_intelligence.digest(context)))
        return formatted_text+'\n\n<prism-memory-context id="'+recall_id+'">\n'+context+'\n</prism-memory-context>'

    def _memory_envelope(self, instruction):
        start=instruction.rfind('\n\n<prism-memory-context id="')
        if start<0:return None
        match=re.fullmatch(r'\n\n<prism-memory-context id="([0-9a-f]{64})">\n([\s\S]*)\n</prism-memory-context>',instruction[start:])
        if not match:return None
        with self.store.db() as db:
            row=db.execute('SELECT * FROM memory_turn_prompts WHERE id=?',(match.group(1),)).fetchone()
        if (row and row['formatted_hash']==memory_intelligence.digest(instruction[:start])
                and row['context_hash']==memory_intelligence.digest(match.group(2))):
            return row,start
        return None

    def strip_memory_for_display(self, instruction):
        found=self._memory_envelope(instruction)
        return instruction[:found[1]] if found else instruction

    def _restore_memory_prompt(self, tid, sid, instruction, token):
        found=self._memory_envelope(instruction)
        if found:
            row,start=found
            if row['task']==tid and row['session']==sid:
                return row['raw_text'],token or row['request_id']
        return instruction,token

    def plan(self, body):
        task = self.store.get(body['taskId'])
        instruction = text(body.get('instruction',''), 8000, True)
        kind = body.get('taskType') or 'auto'
        mode = self.prefs.get(task['owner'])['mode'] if task['owner'] else 'auto'
        plan = routing_policy.build_plan(instruction, mode=mode, provider=provider(task['owner']), task_override=kind, policy=self.policy())
        plan['quotaStates'] = quota_states(self.quota())
        plan['newModelCandidate'] = self.evolution.candidate(plan, self.policy())
        plan['fallbackAdvice'] = routing_policy.choose_fallback(plan, plan['quotaStates'])
        return {'ok': True, 'plan': plan, 'catalog': self.catalog()}

    def observe_input(self, sid, obs):
        from datetime import datetime
        native_text=obs.get('lastUserText'); input_at=obs.get('inputAt',0)
        if not input_at and obs.get('inputTime'):
            try: input_at=datetime.fromisoformat(str(obs['inputTime']).replace('Z','+00:00')).timestamp()
            except (TypeError,ValueError): return
        if not isinstance(input_at,(int,float)) or not input_at or not native_text or is_transcript_notice(native_text): return
        for task in self.store.for_session(sid):
            if task['owner']==sid and (input_at>task['body'].get('lastHumanAt',0)
                    or is_transcript_notice(task['body'].get('lastRequest'))):
                instruction=routing_policy.strip_routing_control(native_text)
                # A known handoff's first native message contains our context
                # envelope. Retain the recorded user input, not another copy of
                # the generated memory prompt, in subsequent handoffs.
                for attempt in self.store.attempts(task['id']):
                    origin=attempt['body']
                    if (attempt['session']==sid and attempt['kind'] in ('handover','start')
                            and instruction.startswith('[PRISM 업무 맥락 v')
                            and origin.get('contextPath') and origin['contextPath'] in instruction
                            and origin.get('instruction') and origin['instruction'] in instruction):
                        instruction=origin['instruction'];break
                self._record_owner_input(task['id'],sid,instruction,input_at,'native-input')

    def _record_owner_input(self, tid, sid, instruction, input_at, source, input_token=''):
        # Check ownership under the same write transaction as the projection.
        # A transfer racing the observation must not overwrite its new context.
        if not isinstance(instruction,str) or isinstance(input_at,bool) or not isinstance(input_at,(int,float)) or not math.isfinite(input_at) or input_at <= 0:
            return
        instruction=routing_policy.strip_routing_control(instruction.rstrip())
        instruction,input_token=self._restore_memory_prompt(tid,sid,instruction,input_token)
        instruction=instruction.strip()
        if not instruction: return
        identity='input:'+fingerprint([sid,input_token or input_at])
        raw_hash=hashlib.sha256(instruction.encode()).hexdigest()
        with self.store.db(True) as db:
            try:self.store._check_input_lease(db,tid,input_token)
            except ValueError:return
            task=self.store.row(db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
            if not task or task['owner']!=sid: return
            existing=work_memory.get_item(db,tid,identity)
            if existing:
                if existing['source'].get('originalSha256') != raw_hash:
                    raise ValueError('같은 입력의 원문이 달라졌어요. 원본 입력을 확인해 주세요.')
                return
            body=task['body']
            if input_at<=body.get('lastHumanAt',0) and not is_transcript_notice(body.get('lastRequest')): return
            if '_auto' not in body and body.get('source') in ('nomad-binding','user-approved-20261018','exact-conversation'):
                body['_auto']={k:body.get(k) for k in ('goal','decisions','constraints','nextAction','openIssues')}
            automatic=body.setdefault('_auto',{})
            provenance={'kind':source,'session':sid,'requestId':input_token,
                        'reference':self._transcript_reference(sid),
                        'originalSha256':raw_hash,'originalChars':len(instruction),
                        'contentTruncated':len(instruction)>100000}
            work_memory.record(db,tid,task['revision']+1,identity,'instruction',identity,'assert',
                               instruction[:100000],provenance,input_at)
            summary=text(instruction[:8000])
            if (not body.get('_manualFields',{}).get('nextAction')
                    and input_at >= body.get('_sourceClock',{}).get('nextAction',0)
                    and (not body.get('nextAction') or body['nextAction']==automatic.get('nextAction'))):
                self.store._replace_memory_field(db,task,'nextAction',summary,provenance,input_at)
                body['nextAction']=summary;automatic['nextAction']=summary
                body.setdefault('_sourceClock',{})['nextAction']=input_at
            body.update(lastRequest=summary,lastHumanAt=input_at,
                        contextSource=body.get('contextSource',source))
            db.execute('UPDATE tasks SET body=?,revision=revision+1,updated=? WHERE id=?',
                       (encoded(body),time.time(),tid))

    def _transcript_reference(self, sid):
        host=provider(sid); uid=sid.split(':',1)[-1]
        meta=getattr(self.hub,'_'+host+'_meta_cache',{}).get(uid,{})
        return meta.get('rollout_path') or meta.get('jsonl_path') or ''

    def _record_owner_result(self, tid, sid, obs):
        key=obs.get('executionKey') or obs.get('turnKey')
        result=obs.get('text')
        if not key or not isinstance(result,str) or not result or is_transcript_notice(result): return
        event_at=obs.get('eventAt') or time.time()
        if isinstance(event_at,bool) or not isinstance(event_at,(int,float)) or not math.isfinite(event_at): return
        identity='report:'+fingerprint([sid,key])
        with self.store.db(True) as db:
            try: self.store._check_input_lease(db,tid)
            except ValueError: return
            task=self.store.row(db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
            if not task or task['owner']!=sid or work_memory.get_item(db,tid,identity): return
            source={'kind':'observed-response','session':sid,'turnKey':str(key),
                    'reference':self._transcript_reference(sid),
                    'verified':False,'originalSha256':hashlib.sha256(result.encode()).hexdigest(),
                    'originalChars':len(result),'contentTruncated':len(result)>100000}
            work_memory.record(db,tid,task['revision']+1,identity,'report',identity,'assert',
                               result[:100000],source,event_at)
            body=task['body']
            if event_at >= body.get('latestResultAt',0):
                body.update(latestResult=text(result[:1600]),latestResultTurn=key,latestResultAt=event_at)
            db.execute('UPDATE tasks SET body=?,revision=revision+1,updated=? WHERE id=?',
                       (encoded(body),time.time(),tid))

    def observe(self, sid, obs):
        self.store.observe(sid, obs)
        self.observe_input(sid,obs)
        for task in self.store.for_session(sid):
            native_text=obs.get('lastUserText'); input_at=obs.get('inputAt',0)
            if task['owner']==sid and is_transcript_notice(task['body'].get('lastRequest')) and not native_text:
                # An old tool notice may outlive the bounded transcript window.
                # Clear only its display projection; preserve source logs and the
                # existing next action instead of inventing a replacement request.
                self.store.sync_context(task['id'],{},task['body'].get('contextSource','native-input'),
                                        activity={'lastRequest':''})
            if task['owner']==sid and obs.get('status')=='completed' and obs.get('text'):
                self._record_owner_result(task['id'],sid,obs)
            for attempt in self.store.attempts(task['id']): self.evolution.settle(attempt)

    def record_send(self, sid, rid, state, body):
        if not body.get('observedFromLedger') and body.get('instruction') and not self.registry.relation(sid):
            task=self.ensure_task(sid,body['instruction'])
            if task['owner']==sid:
                self._record_owner_input(task['id'],sid,body['instruction'],time.time(),'chat',rid)
        for task in self.store.for_session(sid):
            if task['owner'] != sid:
                continue
            self.store.event(task['id'], 'send:' + rid + ':' + state, 'parent_send',
                             {'sessionId':sid, 'state':state, **body})

    def pending_task_inputs(self, task):
        """Include original source identities and recorded redirected destinations."""
        related={task['owner']}|{l['session'] for l in self.store.links(task['id']) if l['role'] in ('responsible','previous')}
        related=sorted(related);slots=','.join('?' for _ in related)
        result={'ledger':[],'queue':[]};ledger=self.root/'send_ledger.sqlite3'
        if ledger.exists():
            with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as db:
                result['ledger']=db.execute("SELECT request_id,state FROM send_ledger WHERE state IN ('pending','unknown') AND (session_id IN ("+slots+") OR json_extract(meta_json,'$.dispatchSessionId')=? OR json_extract(meta_json,'$.taskId')=?) ORDER BY request_id",related+[task['owner'],task['id']]).fetchall()
        queue=self.root/'followups.sqlite3'
        if queue.exists():
            with sqlite3.connect('file:'+str(queue)+'?mode=ro',uri=True) as db:
                for rid,sid,state,payload in db.execute("SELECT id,session,state,payload FROM followups WHERE state IN ('queued','sending','unknown') AND session IN ("+slots+") ORDER BY id",related):
                    result['queue'].append({'id':rid,'session':sid,'state':state,'routingMode':json.loads(payload).get('routingMode')})
        return result

    def pending_review_snapshot(self, task):
        """Carry review evidence across explicit handovers without deciding it."""
        attempts = self.store.attempts(task['id'])
        return {'taskReviewPending': task['state'] == 'review',
                'attempts': sorted([
                    {'id': a['id'], 'state': a['state'],
                     **{k: a['body'].get(k) for k in ('reviewState', 'responseHash', 'responsePath')}}
                    for a in attempts if a['body'].get('reviewState') == 'pending'], key=lambda a: a['id']),
                'legacyPackages': sorted([
                    {'id': p['id'], 'body': p['body']}
                    for p in self.legacy_results(self.store.links(task['id']), attempts)
                    if not p.get('decision')], key=lambda p: p['id'])}

    def handover_ready(self, task, ignore_request='', ignore_queue='', preserve_reviews=False):
        owner=task['owner']; session=self.require_session(owner,True)
        view=self.controls.view(owner,session.get('status','UNKNOWN'))
        obs=self.flow.observer.observe(session,native=True) if self.flow.observer else {}
        if obs.get('status') not in ('completed','failed','interrupted','cancelled'):
            raise NativeNotSent('원 실행의 종료가 확인되지 않아 교대를 보류했어요. 입력은 보내지 않았어요.')
        # A claimed PRISM head item and later auto items are unsent and remain in
        # their original queue. Native/manual/unknown entries cannot be migrated.
        pending_inputs=self.pending_task_inputs(task)
        blocked_queue=[q for q in view.get('queue',[])+pending_inputs['queue'] if q.get('state') in ('queued','sending','unknown')
                       and not (ignore_queue and (q.get('id')==ignore_queue or
                           (q.get('state')=='queued' and q.get('routingMode')=='auto' and not str(q.get('id','')).startswith('codex-app:'))))]
        if view.get('busy') or view.get('requests') or blocked_queue:
            raise NativeNotSent('진행 중 작업·질문·대기 입력이 있어 교대를 보류했어요.')
        if any(rid!=ignore_request for rid,state in pending_inputs['ledger']):
            raise NativeNotSent('기존 입력의 접수 확인이 남아 있어 교대를 보류했어요.')
        if any(a['state'] in PENDING for a in self.store.attempts(task['id'])):
            raise NativeNotSent('기존 세부 작업의 실행·접수 확인이 남아 있어 교대를 보류했어요.')
        if not preserve_reviews and (task['state']=='review' or any(a['body'].get('reviewState')=='pending' for a in self.store.attempts(task['id']))):
            raise NativeNotSent('검토할 결과가 남아 있어 교대를 보류했어요.')
        if any(b.get('autoSynthesizeTo')==owner and not b.get('synthesized') and not b.get('notified') for b in self.registry.list_batches()):
            raise NativeNotSent('원 담당자에게 회수할 결과가 남아 있어 교대를 보류했어요.')

    def prepare_continuation(self, sid, instruction, chosen, mode, delivery, allowed, queue_request_id=''):
        """Preflight only. The original composer ledger owns the eventual send."""
        if mode!='auto' or delivery!='send' or not allowed: return None
        policy=self.policy(); models={m['id']:m for m in policy['models']}
        source=models.get(chosen)
        if not source: return None
        states=quota_states(self.quota())
        if states.get(source['quotaPool'])!='available':
            states=quota_states(self.quota(fresh=True))
        if states.get(source['quotaPool'])!='quota_exhausted':
            context=self.context_meter.snapshot(sid)
            if not context.get('ready'): return None
            # Keep the precise model/effort and subscription. A model changed in
            # the composer invalidates the previous model's measured pressure.
            if not context.get('model') or context['model']!=source.get('model'): return None
            if not any(m['id']==chosen for m in self.catalog().get(source['host'],[])): return None
            task=self.ensure_task(sid,instruction)
            if task['owner']!=sid: raise NativeNotSent('이전 담당 대화예요. 현재 담당 대화에서 이어 주세요.')
            try: self.handover_ready(task,ignore_queue=queue_request_id)
            except NativeNotSent:
                # Context rotation is optional; ordinary native input/queue rules
                # still apply while approvals, workers or a running turn exist.
                return None
            return {'taskId':task['id'],'sourceSession':sid,'source':chosen,'target':source,
                    'reason':'context_pressure','context':context,'queueRequestId':queue_request_id}
        return self._prepare_quota_continuation(sid,instruction,chosen,source,models,states,policy,queue_request_id)

    def validate_quota_instruction(self, instruction, policy):
        plan=routing_policy.build_plan(instruction,policy=policy)
        handover=policy.get('responsibleHandover') or {}
        fiction_allowed=handover.get('enabled') is True and handover.get('allowFiction') is True
        if plan.get('coreDecision') or plan.get('exactMaxRequired') or (plan['taskType'].startswith('fiction') and not fiction_allowed):
            exc=NativeNotSent('이 작업은 지정 모델·검토 조건을 유지해야 해서 자동 교대를 보류했어요. 입력은 보내지 않았어요.')
            exc.permanent=True;raise exc
        # An explicit model/strength in the current instruction always wins.
        if explicit_model_instruction(instruction):
            exc=NativeNotSent('본문에 지정한 모델이 있어 자동 교대를 보류했어요. 입력은 보내지 않았어요.')
            exc.permanent=True;raise exc

    def _prepare_quota_continuation(self, sid, instruction, chosen, source, models, states, policy, queue_request_id=''):
        self.validate_quota_instruction(instruction,policy)
        handover=policy.get('responsibleHandover')
        if handover is not None and not handover['enabled']:
            raise NativeNotSent('자동 담당 교대 정책이 꺼져 있어 입력을 유지했어요.')
        candidates=handover['priority'] if handover is not None else ['claude-opus-5-5:high','MODEL_PLACEHOLDER_M402','gpt-6-astra:high','MODEL_PLACEHOLDER_M417','muse-wonder-full:max']
        catalog=self.catalog()
        target=next((models[m] for m in candidates if m in models and models[m]['quotaPool']!=source['quotaPool']
                     and states.get(models[m]['quotaPool'])=='available'
                     and any(v['id']==m for v in catalog.get(models[m]['host'],[]))),None)
        if not target: raise NativeNotSent('사용 가능한 다른 구독을 확인하지 못했어요. 입력은 그대로예요.')
        task=self.ensure_task(sid,instruction)
        if task['owner']!=sid: raise NativeNotSent('이전 담당 대화예요. 현재 담당 대화에서 이어 주세요.')
        self.handover_ready(task,ignore_queue=queue_request_id)
        return {'taskId':task['id'],'sourceSession':sid,'source':chosen,'target':target,'reason':'official_quota_exhausted','queueRequestId':queue_request_id}

    def plan_quota_recovery(self, sid, instruction):
        """Fresh exhaustion preflight only; the caller owns event dedup and send."""
        if self.prefs.get(sid)['mode']!='auto': return None
        target=self.resolve_input_target(sid,'auto',instruction)
        if target['redirected']: return None
        if not hasattr(self.hub,'routing_default_model'): return None
        chosen=self.hub.routing_default_model(sid)
        policy=self.policy();models={m['id']:m for m in policy['models']}
        self.validate_quota_instruction(instruction,policy)
        source=models.get(chosen)
        if not source: return None
        states=quota_states(self.quota(fresh=True))
        if states.get(source['quotaPool'])!='quota_exhausted': return None
        return self._prepare_quota_continuation(sid,instruction,chosen,source,models,states,policy)

    def continue_send(self, prepared, instruction, request_id, attachments=None):
        try:
            acquired=self.store.acquire_input(prepared['taskId'],prepared['sourceSession'],request_id)
        except ValueError as exc:
            raise NativeNotSent(str(exc)) from exc
        try:
            return self._continue_send(prepared,instruction,request_id,attachments)
        finally:
            if acquired:self.store.release_input(prepared['taskId'],request_id)

    def _continue_send(self, prepared, instruction, request_id, attachments=None):
        try:
            task=self.store.get(prepared['taskId']);owner=task['owner'];target=prepared['target']
            if owner!=prepared['sourceSession']: raise NativeNotSent('이미 담당자가 교대했어요. 현재 대화에서 이어 주세요.')
            if task['state'] in ('done','paused') or self.prefs.get(owner)['mode']!='auto':
                raise NativeNotSent('업무 상태나 수동 모델 선택이 바뀌어 자동 교대를 보류했어요.')
            self.handover_ready(task,request_id,prepared.get('queueRequestId',''))
            if prepared['reason']=='context_pressure':
                current=self.context_meter.snapshot(owner)
                if not current.get('ready') or current.get('model')!=prepared.get('context',{}).get('model'):
                    raise NativeNotSent('원래 앱이 맥락을 갱신했어요. 교대 입력은 보내지 않았어요.')
            session=self.sessions()[owner]
            workspace=session.get('cwd') or session.get('workspace')
            if not workspace or not Path(workspace).is_dir(): raise NativeNotSent('기존 작업 폴더를 확인하지 못해 교대를 보류했어요.')
            prompt,packet=self.context_prompt(task['id'],instruction)
            reason='컨텍스트 길이' if prepared['reason']=='context_pressure' else '주간·단기 한도 소진'
            prompt+='\n\n'+reason+'에 따른 같은 업무의 담당 교대예요. 아래 새 입력은 이전 담당자에게 보내지 않았어요.\n'
            prompt+='이전 승인·실행권·필수 검토·문체 계약을 유지하고, 실행 중이거나 결과 불명인 과거 입력을 다시 수행하지 마세요.\n'
            prompt+='새 사용자 입력:\n'+instruction
            plan=routing_policy.build_plan(instruction,mode='auto',provider=target['host'],policy=self.policy())
            prompt=routing_policy.compose_routed_prompt(prompt,plan)
            aid=self.store.attempt(task['id'],'handover',{'instruction':instruction[:8000],'requestedModel':target['id'],
                'requestedModelLabel':target['label'],'actualModel':'unverified','provider':target['host'],
                'previousOwner':owner,'contextId':packet['id'],'contextPath':packet['path'],
                'automaticContinuation':True,'reason':prepared['reason'],'contextMeasurement':prepared.get('context'),'reviewState':'not_arrived'},'continuation-'+request_id,resume_capacity=True)
        except NativeNotSent:
            raise
        except Exception as exc:
            raise NativeNotSent('담당 교대 준비를 완료하지 못했어요. 새 입력은 아직 보내지 않았어요.') from exc
        execution={};sid='';accepted=False
        try:
            options={}
            if target['host']=='codex' and prepared['reason']=='context_pressure':
                prior=next((a for a in self.store.attempts(task['id']) if a['state']=='not_sent' and a['session']
                    and a['body'].get('previousOwner')==owner and a['body'].get('requestedModel')==target['id']
                    and a['body'].get('reason')=='context_pressure'),None)
                if prior: options['prepared_session']=prior['session']
            sid=self.hub.start_new_session(prompt,workspace_path=workspace,provider=target['host'],model=target['id'],
                custom_title=task['title']+' · 이어서',attachments=attachments or [],execution_record=execution,permission_source=owner,**options)
            accepted=True
            self.store.finish_attempt(aid,'accepted',sid,{'turnId':execution.get('turnId')})
            self.store.link(task['id'],sid,'reference','automatic-continuation:'+request_id)
            if self.store.get(task['id'])['revision']!=task['revision']:
                raise ValueError('업무 맥락이 변경돼 담당 연결을 확인해야 해요.')
            self.store.transfer(task['id'],owner,sid,packet['id'],input_token=request_id)
            if hasattr(self.prefs,'set'): self.prefs.set(sid,'auto')
            return {'sessionId':sid,'previousSessionId':owner,'taskId':task['id'],'attemptId':aid,
                    'autoContinuation':True,'continuationReason':prepared['reason'],'model':target['id'],'provider':target['host'],**execution}
        except Exception as exc:
            sid=sid or execution.get('sessionId') or getattr(exc,'session_id','')
            submitted=accepted or execution.get('submitted',False)
            state='not_sent' if isinstance(exc,NativeNotSent) and not submitted else 'unknown'
            capacity_wait=isinstance(exc,NativeCapacityWait) and not submitted and not sid
            self.store.finish_attempt(aid,state,sid,{'error':str(exc),'capacityDeferred':capacity_wait})
            if state=='not_sent':
                # Only a proven non-submission may continue once in the original
                # chat. Never fall back on a lost acknowledgement or owner change.
                if prepared['reason']=='context_pressure' and self.store.get(task['id'])['owner']==owner:
                    exc.continuation_deferred=True
                raise
            from codex_native_client import NativeOutcomeUnknown
            raise NativeOutcomeUnknown('교대 입력의 접수를 확인하지 못했어요. 같은 업무 기록에서 확인하며 다시 보내지 않아요.') from exc

    def action(self, body):
        action = body.get('action'); tid = body.get('taskId','')
        if action == 'standby_handover':
            return self.standby_handover.enqueue(body)
        if action == 'model_revert':
            self.evolution.revert(body.get('model')); return {'ok':True}
        if action == 'create':
            owner = body.get('owner','')
            if owner:self.require_session(owner, True)
            op = body.get('operationId')
            fresh, previous = self.store.claim(op, body)
            if not fresh:return previous
            return self.store.finish(op, {'ok':True, 'task':self.store.create(body.get('project',''), body.get('title',''), owner, body.get('goal',''), task='task-'+fingerprint(op)[:24])})
        if action == 'checkpoint':
            if body.get('values',{}).get('state') == 'done' and any(not p['decision'] for p in self.legacy_results(self.store.links(tid),self.store.attempts(tid))):
                raise ValueError('이전 실행에서 도착한 결과의 검토가 남아 있어요.')
            item = self.store.update(tid, body.get('revision'), body.get('values',{}))
            return {'ok':True, 'task':item, 'context':self.checkpoint_packet(tid)}
        if action == 'memory_patch':
            result=self.store.patch_memory(tid,body.get('revision'),body.get('operationId'),body.get('patches'))
            return {'ok':True,'result':result,'memory':self.store.memory(tid)}
        if action == 'memory_search':
            options={k:body[k] for k in ('at','known_at','before','after','shared','federated','limit') if k in body}
            result=self.store.memory(tid,body.get('query',''),body.get('budget',6000),**options)
            result['recallId']=memory_intelligence.record_recall(self.store,tid,self.store.get(tid)['owner'],
                'api:'+str(time.time_ns()),body.get('query',''),result['retrieval'])
            return {'ok':True,'memory':result}
        if action == 'memory_learn':
            return {'ok':True,'result':memory_intelligence.learn(self.store,tid,body.get('session',''),
                body.get('revision'),body.get('operationId'),body.get('entries'))}
        if action == 'memory_read':
            from memory_cli import read_item
            return {'ok':True,'item':read_item(self.store,tid,body.get('itemId'),body.get('offset',0),body.get('size',6000))}
        if action == 'memory_feedback':
            return memory_intelligence.feedback(self.store,tid,body.get('session',''),body.get('operationId'),
                body.get('recallId'),body.get('itemId',''),body.get('outcome'),body.get('note',''),patch=body.get('patch'))
        if action == 'memory_skills':
            if body.get('skillId'):
                return {'ok':True,'skill':memory_intelligence.read_skill(self.store,tid,body['skillId'])}
            options={k:body[k] for k in ('at','known_at','before','after','shared','federated','limit','budget') if k in body}
            return {'ok':True,**self.store.skills(tid,body.get('query',''),include_inactive=body.get('includeInactive',False),**options)}
        if action == 'memory_skill_manage':
            if body.get('op') == 'sync' or body.get('sync') is True:
                return memory_intelligence.sync_skills(self.store,tid,body.get('session',''))
            spec = body.get('spec') if isinstance(body.get('spec'),dict) else {
                k:v for k,v in body.items() if k not in ('action','taskId','session','revision','operationId')
            }
            return memory_intelligence.manage_skill(
                self.store,tid,body.get('session',''),body.get('revision'),body.get('operationId'),spec,
            )
        if action == 'memory_stats':
            self.store.get(tid)
            return {'ok':True,**memory_intelligence.stats(self.store,tid)}
        if action == 'memory_cases':
            self.store.get(tid)
            return {'ok':True,**memory_intelligence.cases(self.store,tid,limit=body.get('limit',25))}
        if action == 'memory_links':
            self.store.get(tid)
            return {'ok':True,**memory_intelligence.links(self.store,tid,node_id=body.get('nodeId'),limit=body.get('limit',100))}
        if action == 'memory_capture':
            payload = body.get('payload') if isinstance(body.get('payload'), dict) else {
                k: v for k, v in body.items() if k not in ('action', 'taskId', 'session')
            }
            return memory_intelligence.auto_capture(self.store, tid, body.get('session', ''), payload)
        if action == 'memory_preflight':
            self.store.get(tid)
            return memory_intelligence.preflight_check(
                self.store, tid,
                plan=body.get('plan', ''),
                files=body.get('files'),
                action_type=body.get('actionType'),
            )
        if action == 'memory_capsule':
            self.store.get(tid)
            return memory_intelligence.build_subagent_capsule(
                self.store, tid,
                plan=body.get('plan', ''),
                files=body.get('files'),
                worker=body.get('worker', ''),
                budget=int(body.get('budget', 1200)),
            )
        if action == 'memory_heal_skills':
            self.store.get(tid)
            return memory_intelligence.heal_skills(
                self.store, tid,
                session=body.get('session', ''),
                dry_run=bool(body.get('dryRun', False)),
            )
        if action == 'memory_verify_skill':
            self.store.get(tid)
            return memory_intelligence.verify_skill(
                self.store, tid,
                body.get('session', ''),
                body.get('skillId', ''),
                command=body.get('command'),
                execution_proof=body.get('executionProof'),
                operation_id=body.get('operationId'),
                timeout=float(body.get('timeout', 15.0)),
            )
        if action == 'memory_doctor':
            return memory_intelligence.doctor(self.store)
        if action == 'memory_backup':
            return memory_intelligence.backup(
                self.store,
                dest_dir=body.get('destDir'),
                include_models=bool(body.get('includeModels', False)),
            )
        if action == 'memory_restore':
            return memory_intelligence.restore(
                self.store,
                backup_dir=body.get('backupDir', ''),
            )
        if action == 'memory_repo_benchmark':
            return memory_intelligence.repo_benchmark(work_dir=body.get('workDir'))
        if action == 'memory_audit':
            return memory_intelligence.audit_repo(
                repo_path=body.get('repoPath') or self.store.root,
                base_ref=body.get('baseRef'),
                head_ref=body.get('headRef', 'HEAD'),
                output_format=body.get('format', 'json'),
            )
        if action == 'memory_trajectory':
            self.store.get(tid)
            return {
                'ok': True,
                **memory_intelligence.attribute_trajectory(
                    self.store, tid,
                    body.get('session', ''),
                    body.get('query', ''),
                    prior_primary_ids=body.get('priorPrimaryIds'),
                    persist=bool(body.get('persist', True)),
                ),
            }
        if action == 'memory_benchmark':
            self.store.get(tid)
            return {'ok': True, **memory_intelligence.benchmark(self.store, tid, budget=body.get('budget', 6000))}
        if action == 'memory_inspector':
            self.store.get(tid)
            return {'ok': True, **memory_intelligence.inspector(self.store, tid, budget=body.get('budget', 6000))}
        if action == 'memory_scenes':
            self.store.get(tid)
            return {'ok': True, **memory_intelligence.scenes(self.store, tid)}
        if action == 'memory_consolidate':
            self.store.get(tid)
            return memory_intelligence.consolidate_memories(
                self.store, tid,
                session=body.get('session', ''),
                similarity_threshold=body.get('similarityThreshold', 0.35),
                dry_run=bool(body.get('dryRun', False)),
            )
        if action == 'memory_sync_markdown':
            self.store.get(tid)
            return memory_intelligence.sync_markdown(
                self.store, tid,
                session=body.get('session', ''),
                note_path=body.get('notePath'),
                explicit_user=body.get('explicitUser'),
            )
        if action == 'memory_export':
            self.store.get(tid)
            return memory_intelligence.export_rules(
                self.store, tid,
                targets=body.get('targets'),
                target_dir=body.get('targetDir'),
                write_files=bool(body.get('writeFiles', False)),
                lang=body.get('lang', 'ko'),
            )
        if action == 'memory_backfill':
            sid=body.get('session','')
            links=[l for l in self.store.links(tid) if l['session']==sid and l['role'] in ('responsible','previous')]
            if not links:raise ValueError('이 업무에 연결된 담당 대화만 가져올 수 있어요.')
            messages=self.hub.get_messages(sid,limit=200,mark_read=False).get('messages',[])
            return {'ok':True,'result':memory_intelligence.import_messages(self.store,tid,sid,messages[-200:])}
        if action == 'link':
            self.require_session(body.get('session',''))
            return {'ok':True, 'task':self.store.link(tid, body['session'], body.get('role','reference'))}
        if action == 'adopt_owner':
            self.require_session(body.get('session',''),True)
            if self.store.get(tid)['owner']:raise ValueError('담당자가 있어요. 담당 교대를 이용해 주세요.')
            self.store.transfer(tid, '', body['session'], 'user-selected-existing')
            return {'ok':True}
        if action == 'review':
            self.store.review(tid, body.get('attemptId'), body.get('decision'), body.get('note',''))
            for a in self.store.attempts(tid): self.evolution.settle(a)
            return {'ok':True}
        if action == 'confirm_handover':
            task = self.store.get(tid)
            attempt = next((a for a in self.store.attempts(tid) if a['id'] == body.get('attemptId')), None)
            if not attempt or attempt['kind'] not in ('handover','start') or attempt['state'] != 'completed':
                raise ValueError('실제 응답이 확인된 교대만 연결할 수 있어요.')
            if task['owner'] != attempt['body'].get('previousOwner',''):
                raise ValueError('이후 담당자가 변경됐어요. 현재 업무를 확인해 주세요.')
            if task['owner']:
                view = self.controls.view(task['owner'], self.sessions().get(task['owner'],{}).get('status','UNKNOWN'))
                if view.get('busy') or view.get('requests') or view.get('queue'):
                    raise ValueError('원 담당자의 실행·입력 상태를 먼저 확인해 주세요.')
            self.store.transfer(tid, task['owner'], attempt['session'], attempt['body']['contextId'])
            return {'ok':True}
        if action == 'plan':return self.plan(body)
        if action in ('dispatch','handover','start'):return self.dispatch(body)
        if action == 'context':return {'ok':True, 'context':self.checkpoint_packet(tid,body.get('query',''))}
        raise ValueError('업무 행동을 확인해 주세요.')

    def dispatch(self, body):
        # Keep a handoff's reviewed context stable until native acceptance and
        # owner transfer. Passive NOMAD projection already respects this lease.
        # The operation receipt still owns deduplication after uncertain sends.
        if body.get('action') not in ('handover', 'start'):
            return self._dispatch(body)
        prior = self.store.operation(body.get('operationId'), body)
        if prior is not None:
            return prior
        task = self.store.get(body['taskId'])
        token = body.get('operationId')
        text(token, 128, True)
        acquired = self.store.acquire_input(task['id'], task['owner'], token)
        try:
            return self._dispatch(body, input_token=token)
        finally:
            if acquired:
                self.store.release_input(task['id'], token)

    def _dispatch(self, body, input_token=''):
        prior = self.store.operation(body.get('operationId'), body)
        if prior is not None:return prior
        tid = body['taskId']; task = self.store.get(tid)
        owner = task['owner']
        if body['action'] == 'start':
            if owner:raise ValueError('이미 담당자가 있어요. 담당 교대를 이용해 주세요.')
        else:self.require_session(owner, True)
        instruction = text(body.get('instruction',''), 8000, True)
        acceptance = text(body.get('acceptance') or '원본 근거·변경 결과·검증·남은 문제를 구분해요.', 3000, True)
        scope = text(body.get('scope') or '이번 사용자 지시와 기존 승인 범위 안에서만 진행해요. 원 실행·승인·수동 모델·원본 기록을 보존해요.', 4000, True)
        host, model = body.get('provider'), body.get('model')
        self.quota(fresh=True)
        plan = self.plan({**body, 'instruction':instruction})['plan']
        fallback = None
        if model == 'auto':
            if body['action'] != 'dispatch' or plan['mode'] != 'auto':
                raise ValueError('수동 모드와 담당 교대에서는 정확한 모델을 선택해 주세요.')
            choice = plan['primary']
            fallback = plan.get('fallbackAdvice')
            if plan['quotaStates'].get(choice.get('quotaPool')) == 'quota_exhausted':
                if not fallback or not fallback.get('candidate'):
                    raise ValueError('사용 가능한 다른 구독을 확인하지 못했어요. 자동 호출을 보류해요.')
                choice = fallback['candidate']
            host, model = choice.get('host'), choice.get('id')
            if choice.get('kind') != 'model':
                raise ValueError('이 유형은 현재 담당자가 진행해요. 별도 배분이 필요하면 모델을 직접 선택해 주세요.')
        if not any(m['id']==model for m in self.catalog().get(host, [])):
            raise ValueError('현재 카탈로그의 정확한 모델을 선택해 주세요.')
        session=self.sessions().get(owner,{})
        workspace = text(body.get('workspace') or session.get('workspace') or session.get('cwd') or '', 2000, True)
        if not Path(workspace).is_absolute() or not Path(workspace).is_dir():
            raise ValueError('현재 존재하는 작업 폴더를 지정해 주세요.')
        if body.get('revision') != task['revision']:
            raise ValueError('업무 내용이 바뀌었어요. 최신 맥락을 확인해 주세요.')
        kind = body['action'] if body['action'] in ('handover','start') else 'worker'
        if kind == 'handover':
            if body.get('preservePendingReviews') is True:
                if body.get('pendingReviewSnapshot') != self.pending_review_snapshot(task):
                    raise ValueError('검토 대기 내용이 바뀌었어요. 최신 결과를 확인한 뒤 교대해 주세요.')
                self.handover_ready(task, preserve_reviews=True)
            view = self.controls.view(owner, self.sessions()[owner].get('status','IDLE'))
            observation = self.flow.observer.observe(self.sessions()[owner], native=True) if self.flow.observer else {}
            if observation.get('status') not in ('completed','failed','interrupted','cancelled'):
                raise ValueError('원 실행 종료를 확인한 뒤 교대해 주세요. 연결 미확인은 작업 종료가 아니에요.')
            if view.get('busy') or view.get('requests') or any(q.get('state') in ('queued','sending','unknown') for q in view.get('queue',[])):
                raise ValueError('진행 중 작업·질문·대기 입력을 먼저 확인해 주세요. 교대는 완료 경계에서 해요.')
            ledger = self.root / 'send_ledger.sqlite3'
            if ledger.exists():
                with sqlite3.connect('file:'+str(ledger)+'?mode=ro', uri=True) as db:
                    pending = db.execute("SELECT count(*) FROM send_ledger WHERE session_id=? AND state IN ('pending','unknown')", (owner,)).fetchone()[0]
                if pending:raise ValueError('원 입력의 접수 확인이 남아 있어요. 기존 전송 기록을 먼저 확인해 주세요.')
            if any(b.get('taskId') == tid and b.get('autoSynthesizeTo') and not b.get('synthesized') and not b.get('notified') for b in self.registry.list_batches()):
                raise ValueError('원 담당자에게 회수할 결과가 남아 있어요. 회수 기록을 먼저 확인해 주세요.')
        prompt, packet = self.context_prompt(tid,instruction,writable=kind!='worker')
        # Selecting a model in this action is exact. Quota alternatives are advice for
        # the next unstarted task; accepted/unknown work is never automatically replayed.
        states = quota_states(self.quota())
        model_info = next((m for m in self.policy()['models'] if m['id']==model), {})
        if states.get(model_info.get('quotaPool')) == 'quota_exhausted':
            raise ValueError('선택 모델의 사용량 소진이 확인됐어요. 다른 구독 후보를 선택해 주세요.')
        op = body.get('operationId'); fresh, previous = self.store.claim(op, body)
        if not fresh:return previous
        aid = None
        sid = ''
        accepted = False
        batch = None
        execution = {}
        trial=None
        try:
            expected_aid='attempt-'+fingerprint([tid,op])[:32]
            candidate=plan.get('newModelCandidate')
            explicit_model=bool(re.search(r'(?i)gpt[- ]?\d|claude[- ]|astra|sonnet|fable|luna|opus|haiku|gemini|아스트라|소넷|루나|오푸스|제미나이|페이블',instruction))
            if (body.get('model')=='auto' and not explicit_model and candidate and model==plan['primary'].get('id')
                and states.get(candidate['quotaPool'])=='available' and self.evolution.reserve(candidate['id'],expected_aid)):
                trial=candidate['id'];host,model=candidate['host'],candidate['id']
            aid = self.store.attempt(tid, kind, {'instruction':instruction, 'scope':scope, 'acceptance':acceptance,
                'requestedModel':model, 'requestedModelLabel':next(m.get('label',model) for m in self.catalog()[host] if m['id']==model),
                'provider':host, 'actualModel':'unverified', 'taskType':plan['taskType'],
                'policyVersion':plan['policyVersion'], 'evolutionCandidate':trial, 'contextId':packet['id'], 'contextPath':packet['path'],
                'fallback':fallback, 'requestedSelection':body.get('model'),
                'previousOwner':owner,
                'intentHash':fingerprint([kind,instruction,scope,acceptance,packet['revision']]),
                'reviewState':'not_arrived'}, op)
            if kind == 'worker':
                batch = {'batchId':aid, 'taskId':tid, 'createdAt':time.time(), 'sourceCascadeId':owner,
                         'autoSynthesizeTo':owner if body.get('autoReturn') is True else '',
                         'synthesized':False, 'notified':False, 'silentWorkers':True, 'dispatchPending':True,
                         'label':task['title']+' · '+plan['title'], 'cascadeIds':[]}
                self.registry.put_batch(batch)
                self.hub._recent_broadcast_batches.append(batch)
            prefix = '책임자로 이어서 진행해요.' if kind=='handover' else '원 메인의 세부 작업이에요. 결과를 원 메인에서 검토해요.'
            prompt += f'\n\n{prefix}\n작업: {instruction}\n범위·파일·권한: {scope}\n수락 조건: {acceptance}\n'
            if body.get('preservePendingReviews') is True:
                prompt += ('검수·승인 대기는 그대로 인수해요. 근거 보관본의 pendingReviews와 원 검토 결과를 확인하세요. '
                           '담당 교대를 결과 채택·필수 검토 통과·실행 승인으로 해석하지 마세요.\n')
            if plan.get('guidance'):
                prompt += '작업 기준: '+plan['guidance']+'\n'
            if fallback and fallback.get('draftOnly'):
                prompt += '이 호출은 사용량 대체 초안이에요. 정확한 Max 검토를 대신하거나 최종 판단 완료로 보고하지 마세요.\n'
            prompt += '요청 범위 밖의 쓰기·배포·외부 전송을 하지 마세요. 마지막에는 결과, 검증, 미확인, 원본 파일을 구분해 주세요.\n'
            exact = routing_policy.build_plan(instruction, mode='manual', provider=host, selected_model=model)
            prompt = routing_policy.compose_routed_prompt(prompt, exact)
            sid = self.hub.start_new_session(prompt, workspace_path=workspace, provider=host, model=model,
                custom_title=task['title']+' · '+('담당' if kind=='handover' else plan['title']),
                background_parent=owner if kind=='worker' else '', worker_batch=aid if kind=='worker' else '', execution_record=execution,
                permission_source=owner)
            accepted = True
            if batch is not None:
                batch.update(cascadeIds=[sid], dispatchPending=False)
                self.registry.put_batch(batch)
            self.store.finish_attempt(aid, 'accepted', sid, {'turnId':execution.get('turnId'), 'transport':execution.get('transport')})
            self.store.link(tid, sid, 'worker' if kind=='worker' else 'reference', 'accepted-operation:'+op)
            if kind in ('handover','start'):self.store.transfer(tid, owner, sid, packet['id'], input_token=input_token)
            result = {'ok':True,'state':'accepted','sessionId':sid,'attemptId':aid,'operationId':op,
                      'actualModel':'unverified','taskId':tid}
        except Exception as exc:
            sid = sid or execution.get('sessionId') or getattr(exc, 'session_id', '')
            accepted = accepted or execution.get('submitted', False)
            state = 'not_sent' if isinstance(exc, (NativeNotSent,ValueError)) and not accepted else 'unknown'
            capacity_wait = isinstance(exc, NativeCapacityWait) and not accepted and not sid
            if aid:
                self.store.finish_attempt(aid,state,sid,{'error':str(exc), 'capacityDeferred':capacity_wait})
                for a in self.store.attempts(tid): self.evolution.settle(a)
            elif trial:
                self.evolution.settle({'id':expected_aid,'state':'not_sent','body':{'evolutionCandidate':trial}})
            if sid:
                try:self.store.link(tid,sid,'reference' if kind!='worker' else 'worker','dispatch-recovery:'+op)
                except Exception:logging.exception('Could not link recovery session')
            if batch is not None:
                batch.update(cascadeIds=[sid] if sid else [], dispatchPending=False, dispatchErrors=1, dispatchUnknown=state=='unknown')
                self.registry.put_batch(batch)
            result = {'ok':False,'state':state,'operationId':op,'sessionId':sid,'error':str(exc)}
            if capacity_wait:
                result.update(capacityDeferred=True, waitReason=exc.wait_reason, attemptId=aid)
        return self.store.finish(op, result)

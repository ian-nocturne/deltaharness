#!/usr/bin/env python3
"""Cross-platform local memory tool for the already-authorized task owner."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from work_store import WorkStore
except ImportError:
    from prism_memory.standalone import StandaloneStore as WorkStore
import work_memory
import memory_intelligence as memory


def schema():
    return {
        'learn': {'revision':'from brief', 'operationId':'fresh stable ID for this batch',
                  'entries':[{'kind':'constraint', 'op':'assert', 'id':'stable-meaning-id',
                              'content':'one clear user constraint', 'certainty':'explicit',
                              'sourceRefs':[{'itemId':'input ID from brief/read', 'quote':'exact source substring'}]}]},
        'kinds':['goal','decision','constraint','next_action','issue','fact','hypothesis','experience','case'],
        'ops':['assert','replace','retract','resolve (issue only)'],
        'optionalEntryFields':{'validFrom':'ISO-8601 with timezone or Unix seconds',
                              'validUntil':'exclusive end; omit for indefinite (auto-inferred when explicit Korean/English time bounds exist)',
                              'autoForesight':'true by default; set false to disable temporal foresight inference',
                              'links':['optional target itemId or skillId list for bidirectional [[links]]'],
                              'entities':['names/identifiers'], 'aliases':['paraphrases'],
                              'reason':'why this replaces/retracts the named item',
                              'scope':'task by default; project only for fact/experience/case with shareEvidence',
                              'shareEvidence':{'itemId':'user instruction ID','quote':'explicit sharing instruction'},
                              'skill':{'id':'optional skill:slug','name':'optional kebab-case','title':'optional title',
                                       'description':'optional description','triggers':['keywords']}},
        'experience':{'situation':'when useful', 'steps':['at least 2 concrete actions for skill synthesis'],
                      'outcome':'observed or reported result',
                      'conditions':'specific conditions (never unconditional/always)',
                      'counterexamples':'explicit boundary exceptions/counterexamples'},
        'case':{'title':'optional short pitfall title',
                'situation':'when this pitfall or failure occurs',
                'symptom':'observed error or failure symptom',
                'rootCause':'optional underlying cause',
                'avoidance':['1 to 10 concrete avoidance rules or guardrails'],
                'conditions':'specific conditions where this warning applies (never unconditional/always)',
                'severity':'warning or critical'},
        'skillManage':{'revision':'from brief', 'operationId':'fresh stable ID',
                       'op':'create/improve/promote/degrade/retract',
                       'skillId':'skill:kebab-slug', 'name':'kebab-slug', 'title':'short title',
                       'description':'when and why to use', 'triggers':['keywords'],
                       'experience':{'situation':'when useful','steps':['step 1','step 2'],
                                     'outcome':'result','conditions':'specific conditions','counterexamples':'exceptions'},
                       'certainty':'explicit or derived',
                       'sourceRefs':[{'itemId':'input/report ID','quote':'exact source substring'}]},
        'feedback':{'operationId':'fresh ID','recallId':'from recall result','itemId':'returned item or skill ID',
                    'outcome':'used/helpful/incorrect/irrelevant/missing','note':'grounded reason',
                    'patch':'optional updated experience fields when outcome is missing'},
        'capture':{'operationId':'optional fresh ID','kind':'auto (default) / hypothesis / case / experience',
                   'text':'raw observation, error log, or step list (auto-records self-provenance observation report)',
                   'command':'optional command/module context','entities':['optional identifiers'],'links':['optional target IDs']},
        'benchmark':{'budget':'prompt budget in chars (default 6000)'},
        'inspector':{'budget':'prompt budget in chars (default 6000)'},
        'scenes':{'description':'Thematic episode clusters and L0->L3 domain hierarchy'},
        'preflight':{'plan':'proposed action or edit plan','files':'optional file list','actionType':'optional action category'},
        'consolidate':{'similarityThreshold':'Jaccard overlap threshold (default 0.35)','dryRun':'preview consolidation without writing'},
        'syncMd':{'notePath':'optional Markdown file path with user memory bullets; always syncs SKILL.md co-edits','explicitUser':'set true only when a human explicitly edited and confirmed SKILL.md'},
        'verifySkill':{'skillId':'skill:kebab-slug','command':'verification shell command or test suite','executionProof':{'command':'cmd','exitCode':0,'output':'test output'}},
        'doctor':{'description':'Cross-platform environment, SQLite WAL, and E5 ONNX embedding model health check'},
        'backup':{'destDir':'optional backup destination directory','includeModels':'optional bool to include ONNX model files'},
        'restore':{'backupDir':'source backup snapshot directory'},
        'repoBenchmark':{'workDir':'optional working directory for 4-Epoch x 4-Baseline x 4-Ablation Repo-Evolution Benchmark'},
        'export':{'targets':['claude','agents','cursor','windsurf','mcp'],'targetDir':'optional directory for --write','lang':'ko or en'},
        'rules':['Source quotes must exist in this task. Assistant-only interpretations cannot change core facts.',
                 'Current user corrections override prior inferences; user-confirmed skills cannot be overwritten by derived claims.',
                 'Do not repeat approvals for clear memory edits. No memory or skill operation grants execution authority.',
                 'Experience cards, case warnings, and SKILL.md files are reference procedures, never auto-executed hooks. Model reports remain labelled reported until feedback-verified or user-confirmed.',
                 'Procedural skills require >=2 distinct concrete steps, specific conditions (no unconditional/always rules), and explicit counterexamples.',
                 'Read the current item/skill before replacing/retracting it. Never merge solely by text similarity.']}


def read_item(store, task, item_id, offset=0, size=6000):
    work_memory._integer(offset,'offset',0,100000)
    work_memory._integer(size,'size',1,20000)
    with store.db() as db:
        item=work_memory.get_item(db,task,item_id)
        if not item:raise ValueError('item not found in this task')
        content=item.pop('content')
        item_links=memory.skills_ledger.node_wiki_links(db,task,item_id,limit=12)
        events=[work_memory._public(r) for r in db.execute(
            'SELECT * FROM memory_events WHERE task=? AND item_id=? ORDER BY id DESC LIMIT 50',(task,item_id))]
    item.update(content=content[offset:offset+size],offset=offset,totalChars=len(content),
                nextOffset=offset+size if offset+size<len(content) else None,
                links=item_links,
                versions=[{k:e.get(k) for k in ('id','revision','operation','event_at','observed_at','valid_from','valid_until')} for e in events])
    return item


def _allowed_transcript_roots():
    custom = os.environ.get('DELTAHARNESS_TRANSCRIPT_DIRS') or os.environ.get('PRISM_TRANSCRIPT_DIRS') or ''
    if custom.strip():
        return tuple(Path(p).expanduser() for p in custom.split(os.pathsep) if p.strip())
    home = Path.home()
    return tuple(home / rel for rel in ('.prism-memory/sessions', '.codex/sessions', '.claude/projects', '.muse/sessions'))


def backfill(store, task, session, end_byte=None):
    """Read only a recorded transcript of this task's responsible/previous session."""
    with store.db() as db:
        link=db.execute('SELECT role FROM sessions WHERE task=? AND session=?',(task,session)).fetchone()
        if not link or link['role'] not in ('responsible','previous'):
            raise ValueError('linked responsible/previous session required')
        refs=[]
        for row in db.execute('SELECT source FROM memory_events WHERE task=? ORDER BY id DESC',(task,)):
            source=json.loads(row['source'])
            if source.get('session')==session and source.get('reference'):
                refs.append(source['reference'])
        if not refs:raise ValueError('no observed transcript reference; use PRISM memory_backfill for this linked session')
    path=Path(refs[0]).expanduser()
    allowed=_allowed_transcript_roots()
    if path.is_symlink() or path.suffix!='.jsonl' or not any(path.resolve().is_relative_to(p.resolve()) for p in allowed):
        raise ValueError('unsupported observed transcript format/path')
    size=path.stat().st_size
    end=size if end_byte is None else work_memory._integer(end_byte,'endByte',0,size)
    start=max(0,end-8*1024*1024)
    with path.open('rb') as f:
        f.seek(start); raw=f.read(end-start)
    lines=raw.splitlines(keepends=True)
    position=start
    if start and lines:position+=len(lines.pop(0))
    messages=[];positions=[]
    for line in lines:
        line_start=position;position+=len(line)
        try:obj=json.loads(line)
        except (ValueError,UnicodeDecodeError):continue
        if not isinstance(obj,dict):continue
        msg=obj.get('payload',{}) if obj.get('type')=='response_item' else obj.get('message',{})
        if not isinstance(msg,dict) or msg.get('role') not in ('user','assistant'):continue
        content=msg.get('content')
        if isinstance(content,list):
            content='\n'.join(str(c.get('text','')) for c in content if isinstance(c,dict) and c.get('type') in ('text','input_text','output_text'))
        if isinstance(content,str) and content:
            positions.append(line_start)
            messages.append({'id':obj.get('uuid') or obj.get('id') or '', 'role':msg['role'],
                             'text':content,'timestamp':obj.get('timestamp')})
    result=memory.import_messages(store,task,session,messages[-200:])
    next_end=positions[-200] if len(messages)>200 else start
    result.update(reference=str(path),readBytes=end-start,nextEndByte=next_end or None,
                  messagesOmitted=max(0,len(messages)-200),completeHistory=False)
    return result


def _default_cli_root():
    pkg_dir = Path(__file__).resolve().parent
    try:
        from prism_memory.paths import resolve_default_data_root
        return str(resolve_default_data_root(package_dir=pkg_dir))
    except Exception:
        return str(pkg_dir)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=_default_cli_root())
    parser.add_argument('--task',default='')
    parser.add_argument('--session',default='')
    parser.add_argument('--standalone',action='store_true',help='Bootstrap task automatically if it does not exist')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('schema'); sub.add_parser('brief'); sub.add_parser('stats'); sub.add_parser('scenes'); sub.add_parser('doctor')
    bak_p=sub.add_parser('backup');bak_p.add_argument('--dest-dir',default=None);bak_p.add_argument('--include-models',action='store_true')
    res_p=sub.add_parser('restore');res_p.add_argument('--backup-dir',required=True)
    rb_p=sub.add_parser('repo-benchmark');rb_p.add_argument('--work-dir',default=None)
    aud_p=sub.add_parser('audit');aud_p.add_argument('repo_positional',nargs='?',default=None);aud_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    aud_p.add_argument('--base',default=None);aud_p.add_argument('--head',default='HEAD');aud_p.add_argument('--format',default='json',choices=['json','markdown','sarif']);aud_p.add_argument('--exit-zero',action='store_true')
    resc_p=sub.add_parser('rescue',aliases=['viberescue']);resc_p.add_argument('repo_positional',nargs='?',default=None);resc_p.add_argument('extra_positional',nargs='?',default=None);resc_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    resc_p.add_argument('--write',action='store_true');resc_p.add_argument('--output-dir',default=None);resc_p.add_argument('--format',default='json',choices=['json','markdown','prompt']);resc_p.add_argument('--exit-zero',action='store_true')
    slop_p=sub.add_parser('anti-slop');slop_p.add_argument('repo_positional',nargs='?',default=None);slop_p.add_argument('extra_positional',nargs='?',default=None);slop_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    slop_p.add_argument('--lock',action='store_true');slop_p.add_argument('--force',action='store_true');slop_p.add_argument('--archetype',default='linear-precision');slop_p.add_argument('--format',default='json',choices=['json','markdown','prompt']);slop_p.add_argument('--exit-zero',action='store_true')
    heal_p=sub.add_parser('heal-skills');heal_p.add_argument('--dry-run',action='store_true')
    ver_p=sub.add_parser('verify-skill');ver_p.add_argument('--id',required=True);ver_p.add_argument('--command',default=None)
    ver_p.add_argument('--proof-file',default=None);ver_p.add_argument('--operation-id',default='');ver_p.add_argument('--timeout',type=float,default=15.0)
    pf_p=sub.add_parser('preflight');pf_p.add_argument('--plan',default='');pf_p.add_argument('--files',default='');pf_p.add_argument('--action-type',default='')
    caps_p=sub.add_parser('capsule');caps_p.add_argument('--plan',default='');caps_p.add_argument('--files',default='');caps_p.add_argument('--worker',default='');caps_p.add_argument('--budget',type=int,default=1200)
    cons_p=sub.add_parser('consolidate');cons_p.add_argument('--threshold',type=float,default=0.35);cons_p.add_argument('--dry-run',action='store_true')
    sync_md_p=sub.add_parser('sync-md');sync_md_p.add_argument('--note-path',default=None);sync_md_p.add_argument('--explicit-user',action='store_true')
    bench_p=sub.add_parser('benchmark');bench_p.add_argument('--budget',type=int,default=6000)
    insp_p=sub.add_parser('inspector');insp_p.add_argument('--budget',type=int,default=6000)
    exp_p=sub.add_parser('export');exp_p.add_argument('--targets',default='claude,agents,cursor,windsurf,mcp')
    exp_p.add_argument('--target-dir',default=None);exp_p.add_argument('--write',action='store_true');exp_p.add_argument('--lang',default='ko')
    cap_p=sub.add_parser('capture');cap_p.add_argument('--file',default=None);cap_p.add_argument('--text',default=None)
    cap_p.add_argument('--kind',default='auto');cap_p.add_argument('--command',default='');cap_p.add_argument('--operation-id',default='')
    recall=sub.add_parser('recall');recall.add_argument('--query',default='');recall.add_argument('--at');recall.add_argument('--known-at')
    recall.add_argument('--before');recall.add_argument('--after');recall.add_argument('--shared',action='store_true');recall.add_argument('--federated',action='store_true')
    recall.add_argument('--budget',type=int,default=6000)
    read=sub.add_parser('read');read.add_argument('--id',required=True);read.add_argument('--offset',type=int,default=0);read.add_argument('--size',type=int,default=6000)
    skills_p=sub.add_parser('skills');skills_p.add_argument('--query',default='');skills_p.add_argument('--include-inactive',action='store_true')
    skills_p.add_argument('--at');skills_p.add_argument('--known-at');skills_p.add_argument('--before');skills_p.add_argument('--after')
    skills_p.add_argument('--shared',action='store_true');skills_p.add_argument('--federated',action='store_true');skills_p.add_argument('--limit',type=int,default=50);skills_p.add_argument('--budget',type=int,default=2400)
    skill_p=sub.add_parser('skill');skill_p.add_argument('--id',required=True)
    cases_p=sub.add_parser('cases');cases_p.add_argument('--limit',type=int,default=25)
    links_p=sub.add_parser('links');links_p.add_argument('--id',default=None);links_p.add_argument('--limit',type=int,default=100)
    for name in ('learn','feedback','skill-manage'):
        sub.add_parser(name).add_argument('--file',required=True)
    sub.add_parser('skill-sync')
    idx=sub.add_parser('index');idx.add_argument('--limit',type=int,default=200)
    imp=sub.add_parser('backfill');imp.add_argument('--source-session',required=True);imp.add_argument('--end-byte',type=int)
    args=parser.parse_args()
    if args.action=='schema':return schema()
    if args.action=='repo-benchmark':return memory.repo_benchmark(work_dir=args.work_dir)
    if args.action=='audit':
        target_repo=args.repo_path or args.repo_positional or '.'
        res=memory.audit_repo(target_repo,base_ref=args.base,head_ref=args.head,output_format=args.format)
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    if args.action in ('rescue','viberescue'):
        from prism_memory.viberescue import build_rescue_bundle
        pos_repo=args.extra_positional if args.repo_positional in ('pack','scan') else args.repo_positional
        target_repo=args.repo_path or pos_repo or '.'
        res=build_rescue_bundle(target_repo,write_files=args.write,output_dir=args.output_dir,output_format=args.format)
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    if args.action=='anti-slop':
        from prism_memory.anti_slop import scan_anti_slop, lock_design_system
        is_lock=bool(args.lock or args.repo_positional=='lock')
        pos_repo=args.extra_positional if args.repo_positional in ('scan','lock') else args.repo_positional
        target_repo=args.repo_path or pos_repo or '.'
        if is_lock:
            res=lock_design_system(target_repo,archetype=args.archetype,write_files=True,force=args.force)
        else:
            res=scan_anti_slop(target_repo,output_format=args.format)
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    store=WorkStore(args.root)
    if args.action=='doctor':return memory.doctor(store)
    if args.action=='backup':return memory.backup(store,dest_dir=args.dest_dir,include_models=args.include_models)
    if args.action=='restore':return memory.restore(store,backup_dir=args.backup_dir)
    if not args.task:
        if args.standalone:
            args.task='standalone/main'
        else:
            raise ValueError('task is required (--task <taskId>)')
    if args.standalone:
        task=store.ensure_standalone_task(args.task,session=args.session or 'standalone-owner')
        if not args.session:
            args.session=task['owner']
    else:
        task=store.get(args.task)
    if args.action=='brief':
        result=store.memory(args.task,include_retrieval=False)
        with store.db() as db:
            originals=[work_memory._public(r) for r in db.execute(
                "SELECT * FROM memory_items WHERE task=? AND kind IN ('instruction','report') ORDER BY event_id DESC LIMIT 10",(args.task,))]
        result['originals']=[{'id':r['item_id'],'kind':r['kind'],'excerpt':r['content'][:2000],
                              'totalChars':len(r['content']),'source':r['source']} for r in originals]
        result.update(owner=task['owner'],project=task['project'],note='Use read for exact source quotes beyond the excerpt.')
        return result
    if args.action=='recall':
        result=store.memory(args.task,args.query,args.budget,at=args.at,known_at=args.known_at,
                            before=args.before,after=args.after,shared=args.shared,federated=args.federated)
        result['recallId']=memory.record_recall(store,args.task,args.session,'cli:'+str(time.time_ns()),args.query,result['retrieval'])
        return result
    if args.action=='read':return read_item(store,args.task,args.id,args.offset,args.size)
    if args.action=='skills':
        return store.skills(args.task,args.query,include_inactive=args.include_inactive,
                            at=args.at,known_at=args.known_at,before=args.before,after=args.after,
                            shared=args.shared,federated=args.federated,limit=args.limit,budget=args.budget)
    if args.action=='skill':
        return memory.read_skill(store,args.task,args.id)
    if args.action=='cases':
        return memory.cases(store,args.task,limit=args.limit)
    if args.action=='links':
        return memory.links(store,args.task,node_id=args.id,limit=args.limit)
    if args.action=='stats':
        return memory.stats(store,args.task)
    if args.action=='scenes':
        return memory.scenes(store,args.task)
    if args.action=='preflight':
        return memory.preflight_check(store,args.task,plan=args.plan,files=args.files,action_type=args.action_type)
    if args.action=='capsule':
        return memory.build_subagent_capsule(store,args.task,plan=args.plan,files=args.files,worker=args.worker,budget=args.budget)
    if args.action=='heal-skills' and args.dry_run:
        return memory.heal_skills(store,args.task,session=args.session,dry_run=True)
    if args.action=='consolidate' and args.dry_run:
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=True)
    if args.action=='benchmark':
        return memory.benchmark(store,args.task,budget=args.budget)
    if args.action=='inspector':
        return memory.inspector(store,args.task,budget=args.budget)
    if args.action=='export' and not args.write:
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=False,lang=args.lang)
    if task['owner']!=args.session:raise ValueError('current responsible session ID is required')
    runtime=os.environ.get('CODEX_THREAD_ID')
    if runtime and not os.environ.get('ANTIGRAVITY_AGENT') and args.session!='codex:'+runtime:
        raise ValueError('CLI actor does not match the current native Codex session')
    if os.environ.get('PRISM_PARENT_SESSION_ID'):
        raise ValueError('background worker memory writes must be returned to the responsible session')
    if args.action=='heal-skills':
        return memory.heal_skills(store,args.task,session=args.session,dry_run=False)
    if args.action=='verify-skill':
        proof=None
        if args.proof_file:
            raw=sys.stdin.read(200001) if args.proof_file=='-' else Path(args.proof_file).read_text(encoding='utf-8')
            proof=json.loads(raw)
        return memory.verify_skill(
            store,args.task,args.session,args.id,
            command=args.command,execution_proof=proof,
            operation_id=args.operation_id or None,timeout=args.timeout,
        )
    if args.action=='consolidate':
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=False)
    if args.action=='sync-md':
        return memory.sync_markdown(store,args.task,session=args.session,note_path=args.note_path,explicit_user=True if args.explicit_user else None)
    if args.action=='export':
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=True,lang=args.lang)
    if args.action=='capture':
        if args.file:
            raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
            if len(raw)>200000:raise ValueError('memory request exceeds limit')
            payload=json.loads(raw)
        elif args.text:
            payload={'text':args.text,'kind':args.kind,'command':args.command}
            if args.operation_id:payload['operationId']=args.operation_id
        else:
            raise ValueError('capture requires --text or --file')
        return memory.auto_capture(store,args.task,args.session,payload)
    if args.action=='index':return memory.index_task(store,args.task,args.limit)
    if args.action=='backfill':return backfill(store,args.task,args.source_session,args.end_byte)
    if args.action=='skill-sync':return memory.sync_skills(store,args.task,args.session)
    if args.action in ('learn','feedback','skill-manage'):
        raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
        if len(raw)>200000:raise ValueError('memory request exceeds limit')
        value=json.loads(raw)
        if args.action=='learn':return memory.learn(store,args.task,args.session,value['revision'],value['operationId'],value['entries'])
        if args.action=='skill-manage':
            spec=value.get('spec') if isinstance(value.get('spec'),dict) else {
                k:v for k,v in value.items() if k not in ('revision','operationId')
            }
            return memory.manage_skill(store,args.task,args.session,value['revision'],value['operationId'],spec)
        return memory.feedback(store,args.task,args.session,value['operationId'],value['recallId'],
                               value.get('itemId',''),value['outcome'],value.get('note',''),patch=value.get('patch'))


def cli():
    try:
        result = main()
        exit_zero = result.pop('_cli_exit_zero', False) if isinstance(result, dict) else False
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if not exit_zero and isinstance(result, dict) and 'exitCode' in result:
            return int(result['exitCode'])
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__=='__main__':
    sys.exit(cli())



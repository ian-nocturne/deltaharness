"""Comprehensive regression tests for the PRISM Memory v3 procedural skill loop."""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import work_memory as ledger
import memory_intelligence as memory
import memory_skills as skills_ledger
from prism_memory.standalone import StandaloneStore as WorkStore, StandaloneService as WorkService

OWNER = '19f22446-0dce-4dce-945b-6011b559e10c'
CLI = str(Path(__file__).resolve().parent / 'memory_cli.py')


class MemorySkillsLoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        hub = types.SimpleNamespace(
            sessions_snapshot=lambda: {
                'items': [{'id': OWNER, 'provider': 'antigravity', 'title': 'Memory v3', 'status': 'IDLE'}]
            },
            get_messages=lambda *a, **k: {'messages': []},
        )
        registry = types.SimpleNamespace(relation=lambda sid: None, snapshot=lambda: {}, children=lambda sid: [])
        self.service = WorkService(
            self.root,
            hub,
            registry,
            types.SimpleNamespace(view=lambda *a: {'requests': [], 'queue': []}),
            types.SimpleNamespace(observer=None),
            {},
            types.SimpleNamespace(get=lambda sid: {'mode': 'manual'}),
        )
        self.store = self.service.store
        self.store.create('prism-memory', 'Memory Research', OWNER, goal='Skill loop', task='task-one')
        self.store.create('prism-memory', 'Sibling Task', 'codex:sibling', goal='Sibling', task='task-sibling')
        self.store.create('other-proj', 'Other Project', 'codex:other', goal='Other', task='task-other')
        self.add_source(
            'user-1',
            'instruction',
            '자동 스킬 생성·개선 루프를 구현해. 웹 프로세스만 교체하고 실행 원장을 보존해. 이 절차는 같은 프로젝트에서 공유해.',
            100,
        )
        self.add_source(
            'report-1',
            'report',
            '입력 전달 중인지 확인하고 담당·상태·실행·전송 기록을 저장한 뒤 기존 웹 프로세스만 교체하고 원장을 비교했다.',
            101,
        )

    def add_source(self, item_id, kind, content, event_at=100):
        with self.store.db(True) as db:
            return ledger.record(
                db, 'task-one', 1, 'src:' + item_id, kind, item_id, 'assert',
                content, {'kind': 'test-source'}, event_at,
            )

    def sample_card(self, **overrides):
        base = {
            'situation': 'PRISM 기억·스킬 기능을 운영에 반영할 때 기존 대화와 실행을 보존하기',
            'steps': [
                '현재 입력 전달 중인지 확인하고 담당·상태·실행·전송 기록 스냅샷을 저장한다.',
                '단위·회귀 검사를 통과한 뒤 기존 server.py 웹 프로세스만 재시작한다.',
                '운영 API와 SQLite 원장을 다시 읽어 스냅샷과 일치하는지 검증한다.',
            ],
            'outcome': '기존 담당자·실행·전송 기록 손실 없이 새 기억·스킬 코드가 운영에 반영됨',
            'conditions': '기존 PRISM 배포 경로를 사용하며 진행 중 입력 lease가 없는 경우',
            'counterexamples': '결과 불명(unknown)인 요청을 재전송하거나 현재 DB를 과거 백업으로 덮어쓰지 않음',
        }
        base.update(overrides)
        return base

    def learn_experience(self, item_id='experience:web-cutover', op='assert', card=None,
                         certainty='derived', ref_id='report-1', quote='웹 프로세스만 교체',
                         operation='learn-exp-1', **extra):
        entry = {
            'id': item_id,
            'kind': 'experience',
            'op': op,
            'certainty': certainty,
            'sourceRefs': [{'itemId': ref_id, 'quote': quote}],
            **extra,
        }
        if op not in ('retract', 'resolve'):
            entry['experience'] = card if card is not None else self.sample_card()
        rev = self.store.get('task-one')['revision']
        return memory.learn(self.store, 'task-one', OWNER, rev, operation, [entry])

    def test_auto_skill_synthesis_creates_skill_md_and_provenance(self):
        res = self.learn_experience()
        self.assertIn('skills', res)
        self.assertEqual('skill:web-cutover', res['skills'][0]['skillId'])
        self.assertEqual(1, res['skills'][0]['version'])
        self.assertEqual('reported', res['skills'][0]['evidenceState'])

        skill = memory.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertTrue(skill['diskExists'])
        self.assertTrue(skill['diskVerified'])
        self.assertEqual('web-cutover', skill['name'])
        self.assertEqual(1, len(skill['versions']))

        md_path = Path(skill['skill_path'])
        md_text = md_path.read_text(encoding='utf-8')
        self.assertTrue(md_text.startswith('---\nname: web-cutover\ndescription: "'))
        self.assertIn('## 언제 사용하나요', md_text)
        self.assertIn('## 적용 조건 (Conditions)', md_text)
        self.assertIn('## 적용 예외 및 반례', md_text)
        self.assertIn('## 실행 절차 (Step-by-Step Procedure)', md_text)
        self.assertIn('1. 현재 입력 전달 중인지 확인하고', md_text)

        prov_path = md_path.parent / 'references' / 'provenance.json'
        self.assertTrue(prov_path.exists())
        prov = json.loads(prov_path.read_text(encoding='utf-8'))
        self.assertEqual('skill:web-cutover', prov['skillId'])
        self.assertEqual('report-1', prov['source']['sourceRefs'][0]['itemId'])

    def test_overgeneralization_and_placeholders_are_rejected(self):
        for bad_conditions in ('always', '무조건', '항상', '모든 경우', 'unconditional', 'all cases', 'tbd', '없음'):
            with self.subTest(conditions=bad_conditions), self.assertRaises(ValueError):
                self.learn_experience(
                    item_id='experience:bad-' + str( abs(hash(bad_conditions)) ),
                    card=self.sample_card(conditions=bad_conditions),
                    operation='op-bad-' + str(abs(hash(bad_conditions))),
                )
        for bad_steps in (['tbd', 'step two'], ['same step', 'same step']):
            with self.subTest(steps=bad_steps), self.assertRaises(ValueError):
                self.learn_experience(
                    item_id='experience:bad-steps',
                    card=self.sample_card(steps=bad_steps),
                    skill={'name': 'bad-steps'},
                    operation='op-bad-steps-' + str(len(bad_steps)),
                )

    def test_single_step_experience_stays_experience_unless_skill_explicitly_requested(self):
        one_step = self.sample_card(steps=['단일 단계만 기록한 가벼운 메모'])
        res = self.learn_experience(item_id='experience:one-step', card=one_step, operation='op-one-step')
        self.assertNotIn('skills', res)
        with self.assertRaises(ValueError):
            memory.read_skill(self.store, 'task-one', 'skill:one-step')
        with self.assertRaises(ValueError):
            self.learn_experience(
                item_id='experience:forced-one-step',
                card=one_step,
                skill={'name': 'forced-one-step'},
                operation='op-forced-one-step',
            )

    def test_batch_failure_rolls_back_db_and_does_not_write_skill_files(self):
        rev = self.store.get('task-one')['revision']
        valid_entry = {
            'id': 'experience:rollback-check',
            'kind': 'experience',
            'op': 'assert',
            'sourceRefs': [{'itemId': 'report-1', 'quote': '웹 프로세스만 교체'}],
            'experience': self.sample_card(),
        }
        invalid_entry = {
            'id': 'fact:bad-quote',
            'kind': 'fact',
            'op': 'assert',
            'certainty': 'explicit',
            'content': '없는 인용',
            'sourceRefs': [{'itemId': 'user-1', 'quote': '존재하지 않는 문장입니다'}],
        }
        with self.assertRaises(ValueError):
            memory.learn(self.store, 'task-one', OWNER, rev, 'batch-rollback', [valid_entry, invalid_entry])
        self.assertEqual(rev, self.store.get('task-one')['revision'])
        with self.assertRaises(ValueError):
            memory.read_skill(self.store, 'task-one', 'skill:rollback-check')
        self.assertFalse((self.root / 'memory_skills').exists())

    def test_feedback_promotion_frequency_control_degradation_and_refinement(self):
        self.learn_experience()
        view = self.store.memory('task-one', '웹 프로세스 교체 실행 보존')
        self.assertEqual(1, len(view['retrieval']['skills']))
        recall_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-1', '웹 프로세스 교체', view['retrieval'])

        # 1. First same-session positive feedback promotes reported -> trial-used (version 1 -> 2)
        fb1 = memory.feedback(self.store, 'task-one', OWNER, 'fb-used-1', recall_id, 'skill:web-cutover', 'used', '1차 시험 사용')
        self.assertEqual('trial-used', fb1['skill']['evidenceState'])
        self.assertEqual(2, fb1['skill']['version'])
        self.assertTrue(fb1['skill']['versionChanged'])
        self.assertEqual('promote_trial', fb1['skill']['operation'])

        # 2. Second positive feedback promotes trial-used -> feedback-verified (version 2 -> 3)
        fb2 = memory.feedback(self.store, 'task-one', OWNER, 'fb-helpful-2', recall_id, 'experience:web-cutover', 'helpful', '2차 재사용 검증 완료')
        self.assertEqual('feedback-verified', fb2['skill']['evidenceState'])
        self.assertEqual(3, fb2['skill']['version'])
        self.assertTrue(fb2['skill']['versionChanged'])
        self.assertEqual('promote', fb2['skill']['operation'])
        self.assertEqual({'used': 1, 'helpful': 1, 'incorrect': 0, 'missing': 0}, fb2['skill']['stats'])

        # 2b. Subsequent positive feedback increments counters without version spam (version stays 3)
        fb2b = memory.feedback(self.store, 'task-one', OWNER, 'fb-used-2b', recall_id, 'skill:web-cutover', 'used', '반복 재사용')
        self.assertEqual('feedback-verified', fb2b['skill']['evidenceState'])
        self.assertEqual(3, fb2b['skill']['version'])
        self.assertFalse(fb2b['skill']['versionChanged'])
        self.assertEqual({'used': 2, 'helpful': 1, 'incorrect': 0, 'missing': 0}, fb2b['skill']['stats'])

        # 3. Incorrect feedback degrades skill to needs_revision (version 3 -> 4) and removes from active retrieval
        fb3 = memory.feedback(
            self.store, 'task-one', OWNER, 'fb-incorrect-3', recall_id,
            'skill:web-cutover', 'incorrect', '포트 8765 리스닝 확인 전에 종료 판정하면 안 됨',
        )
        self.assertEqual('needs_revision', fb3['skill']['status'])
        self.assertEqual('needs_revision', fb3['skill']['evidenceState'])
        self.assertEqual(4, fb3['skill']['version'])
        after_degrade = self.store.memory('task-one', '웹 프로세스 교체 실행 보존')
        self.assertEqual([], after_degrade['retrieval']['skills'])
        self.assertFalse(any(r['item_id'] == 'experience:web-cutover' for r in after_degrade['retrieval']['items']))

        # 4. Missing feedback with patch refines and restores active status (version 4 -> 5)
        patched_steps = self.sample_card()['steps'] + ['포트 8765 리스닝과 /api/work 응답을 확인한다.']
        fb4 = memory.feedback(
            self.store, 'task-one', OWNER, 'fb-missing-4', recall_id,
            'skill:web-cutover', 'missing', '포트 리스닝 확인 단계 추가',
            patch={'steps': patched_steps},
        )
        self.assertEqual('active', fb4['skill']['status'])
        self.assertEqual('feedback-verified', fb4['skill']['evidenceState'])
        self.assertEqual(5, fb4['skill']['version'])
        after_restore = self.store.memory('task-one', '웹 프로세스 교체 실행 보존')
        self.assertEqual(1, len(after_restore['retrieval']['skills']))
        self.assertEqual(4, len(after_restore['retrieval']['skills'][0]['steps']))

    def test_user_correction_priority_blocks_derived_overwrite_and_retract(self):
        # Create as reported
        self.learn_experience()
        # User explicitly confirms and updates the procedure
        user_card = self.sample_card(
            conditions='사용자가 승인한 PRISM 운영 배포이며 진행 중 입력 lease가 없을 때만 적용',
        )
        res = self.learn_experience(
            op='replace',
            card=user_card,
            certainty='explicit',
            ref_id='user-1',
            quote='웹 프로세스만 교체하고 실행 원장을 보존해.',
            operation='learn-user-confirm',
        )
        self.assertEqual('user-confirmed', res['skills'][0]['evidenceState'])
        self.assertEqual(2, res['skills'][0]['version'])

        # Derived claim cannot overwrite user-confirmed skill
        with self.assertRaises(ValueError):
            self.learn_experience(
                op='replace',
                card=self.sample_card(conditions='임의의 테스트 환경일 때 적용'),
                certainty='derived',
                ref_id='report-1',
                quote='웹 프로세스만 교체',
                operation='learn-derived-overwrite',
            )

        # Derived claim cannot retract user-confirmed skill
        with self.assertRaises(ValueError):
            self.learn_experience(
                op='retract',
                certainty='derived',
                ref_id='report-1',
                quote='웹 프로세스만 교체',
                operation='learn-derived-retract',
            )

        # Unconfirmed feedback cannot degrade user-confirmed skill
        view = self.store.memory('task-one', '웹 프로세스 교체')
        recall_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-u', '웹 프로세스 교체', view['retrieval'])
        with self.assertRaises(ValueError):
            memory.feedback(self.store, 'task-one', OWNER, 'fb-unconfirmed-degrade', recall_id, 'skill:web-cutover', 'incorrect', '임의 실패')

        # Explicit user instruction CAN update or retract user-confirmed skill
        retracted = self.learn_experience(
            op='retract',
            certainty='explicit',
            ref_id='user-1',
            quote='자동 스킬 생성·개선 루프를 구현해.',
            operation='learn-user-retract',
        )
        self.assertEqual('retracted', retracted['skills'][0]['status'])
        skill = memory.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertEqual('retracted', skill['status'])
        self.assertEqual(3, len(skill['versions']))

    def test_idempotent_replay_and_duplicate_substance_deduplication(self):
        rev = self.store.get('task-one')['revision']
        first = self.learn_experience(operation='op-idempotent')
        replayed = memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-idempotent',
            [{
                'id': 'experience:web-cutover',
                'kind': 'experience',
                'op': 'assert',
                'certainty': 'derived',
                'sourceRefs': [{'itemId': 'report-1', 'quote': '웹 프로세스만 교체'}],
                'experience': self.sample_card(),
            }],
        )
        self.assertEqual(first, replayed)

        # New operation with identical substance does not increment skill version
        second = self.learn_experience(op='replace', operation='op-same-substance')
        self.assertEqual(1, second['skills'][0]['version'])
        self.assertTrue(second['skills'][0]['replayed'])

    def test_reuse_in_augment_turn_context_prompt_and_obsidian_projection(self):
        self.learn_experience()
        augmented = self.service.augment_turn(
            OWNER, '웹 프로세스만 교체해서 배포할 때 절차가 뭐야?', '웹 프로세스만 교체해서 배포할 때 절차가 뭐야?', 'turn-req-1',
        )
        self.assertIn('<prism-memory-context', augmented)
        self.assertIn('재사용 절차 스킬:', augmented)
        self.assertIn('skill:web-cutover', augmented)

        prompt, packet = self.service.context_prompt('task-one', '웹 프로세스 교체 절차')
        self.assertIn('재사용 절차 스킬 [skill:web-cutover v1 · reported]', prompt)
        self.assertTrue(packet['body']['memory']['skills'])

        note_path = Path(self.service.publish_task_note(self.store.get('task-one'), self.store.links('task-one'), []))
        note_text = note_path.read_text(encoding='utf-8')
        self.assertIn('## 자동 생성·개선된 절차 스킬', note_text)
        self.assertIn('web-cutover/SKILL.md', note_text)

    def test_project_scoped_skill_sharing_and_bitemporal_expiry(self):
        now = time.time()
        self.learn_experience(
            item_id='experience:shared-proc',
            scope='project',
            shareEvidence={'itemId': 'user-1', 'quote': '이 절차는 같은 프로젝트에서 공유해.'},
            validFrom=now - 100,
            validUntil=now + 100,
            operation='op-shared-proc',
        )
        # Visible in sibling task of same project only when shared=True
        self.assertEqual([], self.store.memory('task-sibling', '웹 프로세스 교체', shared=False)['retrieval']['skills'])
        shared_skills = self.store.memory('task-sibling', '웹 프로세스 교체', shared=True)['retrieval']['skills']
        self.assertEqual(1, len(shared_skills))
        self.assertEqual('task-one', shared_skills[0]['sharedFromTask'])
        self.assertEqual('project-explicit', shared_skills[0]['sharedMode'])

        # Never visible in another project
        self.assertEqual([], self.store.memory('task-other', '웹 프로세스 교체', shared=True)['retrieval']['skills'])

        # Expired at future timestamp
        future_skills = self.store.memory('task-one', '웹 프로세스 교체', at=now + 200)['retrieval']['skills']
        self.assertEqual([], future_skills)

    def test_service_actions_and_cli_commands_and_append_only_triggers(self):
        # Create legacy experience without skill row, then sync via CLI
        with self.store.db(True) as db:
            card = self.sample_card()
            ledger.record(
                db, 'task-one', 2, 'legacy-exp', 'experience', 'experience:legacy-cutover', 'assert',
                '\n'.join([card['situation'], ' / '.join(card['steps']), card['outcome'], '조건: ' + card['conditions'], '예외: ' + card['counterexamples']]),
                {
                    'kind': 'agent-extraction', 'session': OWNER, 'evidenceState': 'reported',
                    'experience': card,
                    'sourceRefs': [{'itemId': 'report-1', 'eventId': 2, 'kind': 'report', 'quote': '웹 프로세스만 교체', 'contentSha256': 'abc'}],
                },
                105,
            )
        # Scrub the host agent's session identity so the fixed test OWNER is accepted inside Codex workers.
        child_env = {k: v for k, v in os.environ.items() if k not in ('CODEX_THREAD_ID', 'PRISM_PARENT_SESSION_ID')}
        sync_out = subprocess.check_output(
            [sys.executable, CLI, '--root', str(self.root), '--task', 'task-one', '--session', OWNER, 'skill-sync'],
            text=True, env=child_env,
        )
        sync_data = json.loads(sync_out)
        self.assertEqual('skill:legacy-cutover', sync_data['synthesized'][0]['skillId'])

        # Test WorkService.action('memory_skill_manage') and 'memory_skills'
        rev = self.store.get('task-one')['revision']
        manage_res = self.service.action({
            'action': 'memory_skill_manage',
            'taskId': 'task-one',
            'session': OWNER,
            'revision': rev,
            'operationId': 'api-skill-promote-1',
            'op': 'promote',
            'skillId': 'skill:legacy-cutover',
            'certainty': 'explicit',
            'sourceRefs': [{'itemId': 'user-1', 'quote': '웹 프로세스만 교체하고 실행 원장을 보존해.'}],
            'reason': '사용자 명시 확인',
        })
        self.assertTrue(manage_res['ok'])
        self.assertEqual('user-confirmed', manage_res['skill']['evidenceState'])
        self.assertEqual(2, manage_res['skill']['version'])

        detail_res = self.service.action({
            'action': 'memory_skills',
            'taskId': 'task-one',
            'skillId': 'skill:legacy-cutover',
        })
        self.assertTrue(detail_res['skill']['diskVerified'])

        # SQL-level immutability of memory_skill_events
        with self.store.db(True) as db:
            for stmt in ("UPDATE memory_skill_events SET status='hacked'", 'DELETE FROM memory_skill_events'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(stmt)

    def test_symlink_skill_folder_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'memory_skills').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.learn_experience(operation='op-symlink-reject')

    def test_bitemporal_known_at_preserves_prior_skill_version(self):
        with patch.object(skills_ledger.time, 'time', return_value=1000.0):
            self.learn_experience(operation='op-v1-at-1000')
        with patch.object(skills_ledger.time, 'time', return_value=2000.0):
            self.learn_experience(
                op='replace',
                card=self.sample_card(outcome='2차 개선된 운영 결과 검증 완료'),
                operation='op-v2-at-2000',
            )
        at_1500 = self.store.skills('task-one', known_at=1500.0)['skills']
        at_2500 = self.store.skills('task-one', known_at=2500.0)['skills']
        self.assertEqual(1, at_1500[0]['version'])
        self.assertEqual(2, at_2500[0]['version'])
        self.assertIn('2차 개선된', at_2500[0]['outcome'])

    def test_owner_revision_and_input_lease_guards_on_skill_manage(self):
        rev = self.store.get('task-one')['revision']
        spec = {
            'op': 'create',
            'skillId': 'skill:guarded',
            'experience': self.sample_card(),
            'sourceRefs': [{'itemId': 'report-1', 'quote': '웹 프로세스만 교체'}],
        }
        with self.assertRaises(ValueError):
            memory.manage_skill(self.store, 'task-one', 'codex:not-owner', rev, 'guard-owner', spec)
        with self.assertRaises(ValueError):
            memory.manage_skill(self.store, 'task-one', OWNER, rev + 99, 'guard-rev', spec)
        self.store.acquire_input('task-one', OWNER, 'lease-token-1')
        try:
            with self.assertRaises(ValueError):
                memory.manage_skill(self.store, 'task-one', OWNER, rev, 'guard-lease', spec)
        finally:
            self.store.release_input('task-one', 'lease-token-1')

    def test_semantic_paraphrase_retrieves_skill_with_local_e5(self):
        (self.root / 'memory_settings.json').write_text('{"semanticEnabled":true,"indexBatch":24}')
        self.learn_experience()
        res = self.store.memory('task-one', '서비스 중단 없이 안전하게 서버 프로세스를 재기동하는 방법')['retrieval']
        self.assertTrue(res['skills'])
        self.assertEqual('skill:web-cutover', res['skills'][0]['skillId'])

    def test_cross_session_immediate_promotion_to_feedback_verified(self):
        # Created by OWNER (reported, v1)
        self.learn_experience()
        # Transfer ownership to a new session and recall
        next_owner = 'codex:next-owner-session'
        self.store.link('task-one', next_owner, 'reference', 'handover-test')
        self.store.transfer('task-one', OWNER, next_owner, 'ctx-1')
        view = self.store.memory('task-one', '웹 프로세스 교체 실행 보존')
        recall_id = memory.record_recall(self.store, 'task-one', next_owner, 'req-cross-1', '웹 프로세스 교체', view['retrieval'])
        # First positive feedback from a different session immediately promotes reported -> feedback-verified
        fb = memory.feedback(self.store, 'task-one', next_owner, 'fb-cross-1', recall_id, 'skill:web-cutover', 'used', '교대 세션에서 재사용 확인')
        self.assertEqual('feedback-verified', fb['skill']['evidenceState'])
        self.assertEqual(2, fb['skill']['version'])
        self.assertEqual('promote', fb['skill']['operation'])

    def test_project_verified_candidate_auto_sharing_and_isolation(self):
        # Create task-scoped skill (reported) and task-scoped constraint in task-one
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-constraint-and-skill',
            [
                {
                    'id': 'constraint:local-only',
                    'kind': 'constraint',
                    'op': 'assert',
                    'certainty': 'explicit',
                    'content': '웹 프로세스만 교체하고 실행 원장을 보존해.',
                    'sourceRefs': [{'itemId': 'user-1', 'quote': '웹 프로세스만 교체하고 실행 원장을 보존해.'}],
                },
                {
                    'id': 'experience:auto-candidate',
                    'kind': 'experience',
                    'op': 'assert',
                    'certainty': 'derived',
                    'sourceRefs': [{'itemId': 'report-1', 'quote': '웹 프로세스만 교체'}],
                    'experience': self.sample_card(),
                },
            ],
        )
        # 1. While reported, task-scoped skill is NOT auto-shared to sibling task even with shared=True
        sib_reported = self.store.memory('task-sibling', '웹 프로세스 교체', shared=True)['retrieval']
        self.assertEqual([], sib_reported['skills'])

        # 2. Promote to trial-used (1st same-session feedback): still NOT auto-shared to sibling task
        view = self.store.memory('task-one', '웹 프로세스 교체')
        recall_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-cand-1', '웹 프로세스 교체', view['retrieval'])
        memory.feedback(self.store, 'task-one', OWNER, 'fb-cand-1', recall_id, 'skill:auto-candidate', 'used', '1차 시험')
        sib_trial = self.store.memory('task-sibling', '웹 프로세스 교체', shared=True)['retrieval']
        self.assertEqual([], sib_trial['skills'])

        # 3. Promote to feedback-verified (2nd positive feedback): NOW auto-shared as project-verified-candidate when shared=True!
        memory.feedback(self.store, 'task-one', OWNER, 'fb-cand-2', recall_id, 'skill:auto-candidate', 'helpful', '2차 검증')
        sib_verified = self.store.memory('task-sibling', '웹 프로세스 교체', shared=True)['retrieval']
        self.assertEqual(1, len(sib_verified['skills']))
        self.assertEqual('skill:auto-candidate', sib_verified['skills'][0]['skillId'])
        self.assertEqual('task-one', sib_verified['skills'][0]['sharedFromTask'])
        self.assertEqual('project-verified-candidate', sib_verified['skills'][0]['sharedMode'])
        self.assertIn('프로젝트 검증 후보:task-one', sib_verified['skills'][0]['excerpt'])
        # Constraints are NEVER shared across tasks
        self.assertFalse(any(r['kind'] == 'constraint' for r in sib_verified['items']))
        # shared=False remains strictly task-isolated
        self.assertEqual([], self.store.memory('task-sibling', '웹 프로세스 교체', shared=False)['retrieval']['skills'])
        # Another project never receives it
        self.assertEqual([], self.store.memory('task-other', '웹 프로세스 교체', shared=True)['retrieval']['skills'])

    def test_semantic_and_lexical_skill_consolidation_merges_duplicate_skills(self):
        self.learn_experience(item_id='experience:cutover-v1', operation='op-cons-1')
        # Learn a second closely related experience with an extra step and extra counterexample
        variant_card = self.sample_card(
            situation='PRISM 기억·스킬 기능을 운영에 배포할 때 기존 대화와 실행 원장을 안전하게 보존하기',
            steps=self.sample_card()['steps'] + ['포트 8765 리스닝과 /api/work 응답 정상 여부를 추가로 점검한다.'],
            counterexamples='포트 충돌 상태에서 강제 종료하거나 백업 DB로 덮어쓰지 않음',
        )
        res2 = self.learn_experience(
            item_id='experience:cutover-v2-variant',
            card=variant_card,
            operation='op-cons-2',
        )
        self.assertEqual('skill:cutover-v1', res2['skills'][0]['skillId'])
        self.assertEqual('skill:cutover-v1', res2['skills'][0]['consolidatedInto'])
        self.assertEqual(2, res2['skills'][0]['version'])
        self.assertIn('experience:cutover-v2-variant', res2['skills'][0]['consolidatedFrom'])

        # Only 1 active skill exists in the task, with 4 merged steps
        all_skills = self.store.skills('task-one')['skills']
        self.assertEqual(1, len(all_skills))
        self.assertEqual(4, len(all_skills[0]['steps']))
        self.assertIn('포트 8765 리스닝과', all_skills[0]['steps'][3])

    def test_staleness_guard_detects_referenced_file_drift_and_30d_inactivity(self):
        helper_file = self.root / 'deploy_helper.py'
        helper_file.write_text('print("v1")\n', encoding='utf-8')
        card = self.sample_card(
            steps=[
                f'먼저 {helper_file} 스크립트로 입력 전달 상태와 원장 스냅샷을 점검한다.',
                '단위·회귀 검사를 통과한 뒤 기존 server.py 웹 프로세스만 재시작한다.',
                '운영 API와 SQLite 원장을 다시 읽어 스냅샷과 일치하는지 검증한다.',
            ],
        )
        self.learn_experience(item_id='experience:file-tracked', card=card, operation='op-stale-1')
        fresh_skill = memory.read_skill(self.store, 'task-one', 'skill:file-tracked')
        self.assertFalse(fresh_skill['stale'])
        self.assertEqual(1, fresh_skill['staleness']['referencedFileCount'])

        # Modify the referenced file -> staleness guard triggers
        helper_file.write_text('print("v2 - modified implementation")\n', encoding='utf-8')
        drifted_skill = memory.read_skill(self.store, 'task-one', 'skill:file-tracked')
        self.assertTrue(drifted_skill['stale'])
        self.assertTrue(any(r.startswith('referenced_file_changed:deploy_helper.py') for r in drifted_skill['staleness']['reasons']))

        # Retrieval applies staleDecay and adds '재확인 권장' badge
        ret = self.store.memory('task-one', '웹 프로세스 재시작 스냅샷')['retrieval']
        self.assertTrue(ret['skills'][0]['stale'])
        self.assertEqual(-0.005, ret['skills'][0]['signals']['staleDecay'])
        self.assertIn('재확인 권장', ret['skills'][0]['excerpt'])

        # Context prompt and Obsidian note also surface '재확인 권장'
        prompt, _ = self.service.context_prompt('task-one', '웹 프로세스 재시작')
        self.assertIn('재확인 권장', prompt)
        note_path = Path(self.service.publish_task_note(self.store.get('task-one'), self.store.links('task-one'), []))
        self.assertIn('⚠️ 재확인 권장', note_path.read_text(encoding='utf-8'))

        # Test 30-day inactivity staleness
        eval_35d = skills_ledger.evaluate_staleness(fresh_skill, now=time.time() + 35 * 86400)
        self.assertTrue(eval_35d['stale'])
        self.assertIn('inactive_30d', eval_35d['reasons'])

    def test_memory_stats_effectiveness_dashboard_cli_and_service_action(self):
        self.learn_experience()
        self.service.augment_turn(OWNER, '웹 프로세스 교체 절차 알려줘', '웹 프로세스 교체 절차 알려줘', 'turn-stats-1')
        view = self.store.memory('task-one', '웹 프로세스 교체')
        recall_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-stats-1', '웹 프로세스 교체', view['retrieval'])
        memory.feedback(self.store, 'task-one', OWNER, 'fb-stats-1', recall_id, 'skill:web-cutover', 'used', '사용 완료')

        api_stats = self.service.action({'action': 'memory_stats', 'taskId': 'task-one'})
        self.assertTrue(api_stats['ok'])
        self.assertGreaterEqual(api_stats['recalls'], 2)
        self.assertEqual(1, api_stats['turnPrompts'])
        self.assertGreater(api_stats['avgPromptChars'], 100)
        self.assertEqual({'trial-used': 1}, api_stats['skillsByEvidenceState'])
        self.assertGreaterEqual(api_stats['totalSkillImpressions'], 2)
        self.assertGreater(api_stats['overallAdoptionRate'], 0.0)
        self.assertEqual(1, len(api_stats['perSkill']))
        self.assertEqual('skill:web-cutover', api_stats['perSkill'][0]['skillId'])

        cli_out = subprocess.check_output(
            [sys.executable, CLI, '--root', str(self.root), '--task', 'task-one', 'stats'],
            text=True,
        )
        cli_stats = json.loads(cli_out)
        self.assertEqual(api_stats['perSkill'], cli_stats['perSkill'])

    def test_v32_failure_case_track_and_pitfall_auto_synthesis_from_incorrect_feedback(self):
        # 1. Overgeneralized case condition ('항상') is rejected
        rev = self.store.get('task-one')['revision']
        with self.assertRaises(ValueError):
            memory.learn(
                self.store, 'task-one', OWNER, rev, 'op-bad-case',
                [{
                    'id': 'case:bad-unconditional',
                    'kind': 'case',
                    'op': 'assert',
                    'certainty': 'derived',
                    'sourceRefs': [{'itemId': 'report-1', 'quote': '웹 프로세스만 교체'}],
                    'case': {
                        'title': '잘못된 무조건 함정 카드',
                        'situation': 'PRISM 운영 웹 서버를 재시작할 때 발생하는 포트 충돌 상황',
                        'symptom': '8765 포트가 이미 사용 중이어서 새 프로세스가 기동 직후 종료됨',
                        'avoidance': ['기존 리스닝 PID를 먼저 확인하고 안전하게 교체한다.'],
                        'conditions': '항상 무조건 적용',
                    },
                }],
            )

        # 2. Valid explicit case card is stored and surfaced as 1st-class warning
        learn_case = memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-good-case',
            [{
                'id': 'case:lease-race-pitfall',
                'kind': 'case',
                'op': 'assert',
                'certainty': 'derived',
                'sourceRefs': [{'itemId': 'report-1', 'quote': '입력 전달 중인지 확인'}],
                'case': {
                    'title': '입력 전달 lease 활성 중 웹 프로세스 재시작 함정',
                    'situation': 'PRISM 운영 배포 중 사용자 입력 전달 lease가 남아 있는 상태',
                    'symptom': '진행 중인 응답 수신이 끊기거나 접수 상태가 unknown으로 남음',
                    'rootCause': 'input_leases 만료·해제 전에 웹 프로세스를 종료함',
                    'avoidance': [
                        'input_leases 테이블에 활성 토큰이 없는지 먼저 확인한다.',
                        '진행 중 입력이 있으면 완료 후 웹 프로세스를 교체한다.',
                    ],
                    'conditions': 'PRISM 운영 웹 프로세스 교체 시 input_leases가 존재할 때만 적용',
                    'severity': 'critical',
                },
            }],
        )
        self.assertEqual('case', learn_case['changes'][0]['kind'])

        # 3. Incorrect feedback on a procedural skill auto-synthesizes case:pitfall-<skill_name>
        self.learn_experience()
        view = self.store.memory('task-one', '웹 프로세스 교체')
        recall_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-case-fb', '웹 프로세스 교체', view['retrieval'])
        fb = memory.feedback(
            self.store, 'task-one', OWNER, 'fb-pitfall-synth', recall_id, 'skill:web-cutover',
            'incorrect', '백그라운드 워커 회수 대기 중 재시작하면 회수 알림이 누락됨',
        )
        self.assertEqual('case:pitfall-web-cutover', fb.get('synthesizedCaseId'))

        # Verify cases appear in retrieval, augment_turn, context_prompt, CLI, API, and Obsidian note
        ret = self.store.memory('task-one', '웹 프로세스 입력 전달 함정')['retrieval']
        case_ids = [c['item_id'] for c in ret['cases']]
        self.assertIn('case:lease-race-pitfall', case_ids)
        self.assertIn('case:pitfall-web-cutover', case_ids)

        aug = self.service.augment_turn(OWNER, '입력 전달 중 웹 프로세스 교체 주의점', '입력 전달 중 웹 프로세스 교체 주의점', 'req-case-aug')
        self.assertIn('주의 함정 사례:', aug)
        self.assertIn('case:lease-race-pitfall', aug)

        prompt, _ = self.service.context_prompt('task-one', '입력 전달 중 웹 프로세스 교체')
        self.assertIn('주의 함정 사례', prompt)

        api_cases = self.service.action({'action': 'memory_cases', 'taskId': 'task-one'})
        self.assertEqual(2, len(api_cases['cases']))

        note_path = Path(self.service.publish_task_note(self.store.get('task-one'), self.store.links('task-one'), []))
        self.assertIn('## 주의할 실패·함정 사례', note_path.read_text(encoding='utf-8'))

    def test_v32_temporal_foresight_auto_valid_from_and_valid_until_extraction(self):
        base_ts = 1792422000.0
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'user-temporal', 'instruction', 'user-temporal', 'assert',
                '앞으로 3시간 동안만 배포 동결 제약을 유지하고, 영구 원칙은 계속 보존해.',
                {'kind': 'chat', 'session': OWNER}, event_at=base_ts,
            )
        rev = self.store.get('task-one')['revision']
        res = memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-foresight',
            [
                {
                    'id': 'fact:temp-freeze',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'explicit',
                    'content': '앞으로 3시간 동안만 배포 동결 제약을 유지',
                    'foresightBaseTime': base_ts,
                    'sourceRefs': [{'itemId': 'user-temporal', 'quote': '앞으로 3시간 동안만 배포 동결 제약을 유지'}],
                },
                {
                    'id': 'fact:permanent-rule',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'explicit',
                    'content': '영구 원칙은 계속 보존해.',
                    'foresightBaseTime': base_ts,
                    'sourceRefs': [{'itemId': 'user-temporal', 'quote': '영구 원칙은 계속 보존해.'}],
                },
            ],
        )
        temp_change = next(c for c in res['changes'] if c['id'] == 'fact:temp-freeze')
        perm_change = next(c for c in res['changes'] if c['id'] == 'fact:permanent-rule')
        self.assertEqual(base_ts, temp_change['validFrom'])
        self.assertEqual(base_ts + 3 * 3600, temp_change['validUntil'])
        self.assertNotIn('foresight', perm_change)

        # Active 1 hour after base_ts, automatically expired 4 hours after base_ts
        active_1h = self.store.memory('task-one', '배포 동결', at=base_ts + 3600)['retrieval']['items']
        self.assertTrue(any(r['item_id'] == 'fact:temp-freeze' for r in active_1h))
        expired_4h = self.store.memory('task-one', '배포 동결', at=base_ts + 4 * 3600)['retrieval']['items']
        self.assertFalse(any(r['item_id'] == 'fact:temp-freeze' for r in expired_4h))

        # Also test explicit date bound ("2026-10-25 18:00까지") and "오늘 자정까지"
        inferred_iso = memory.infer_temporal_bounds('2026-10-25 18:00까지 임시 캐시 모드 유지', base_time=base_ts)
        self.assertIsNotNone(inferred_iso['validUntil'])
        inferred_today = memory.infer_temporal_bounds('오늘 자정까지 긴급 모니터링 유지', base_time=base_ts)
        self.assertGreater(inferred_today['validUntil'], base_ts)

    def test_v32_progressive_disclosure_two_stage_primary_and_index_toc(self):
        # Create 5 distinct facts and 3 distinct skills matching '아키텍처'
        with self.store.db(True) as db:
            for idx in range(1, 6):
                ledger.record(
                    db, 'task-one', 1, f'rep-prog-{idx}', 'report', f'rep-prog-{idx}', 'assert',
                    f'아키텍처 점검 보고서 {idx}단계: 모듈 경계와 상태 원장 검증 항목 {idx}번 확인 완료.',
                    {'kind': 'observed-response', 'session': OWNER}, event_at=100 + idx,
                )
        distinct_cards = [
            {
                'situation': '아키텍처 라우팅 정책 테이블과 쿼터 풀 매핑을 점검할 때',
                'steps': [
                    'routing_profiles.json의 모델 카탈로그와 풀 매핑 키를 파싱한다.',
                    'quota_states 함수로 실시간 잔여 한도 상태 전이를 대조한다.',
                ],
                'outcome': '라우팅 쿼터 매핑 무결성 확인',
                'conditions': '아키텍처 라우팅 프로필 변경 직후에만 적용',
                'counterexamples': '수동 모드 고정 세션에서는 자동 폴백을 실행하지 않음',
            },
            {
                'situation': '아키텍처 SQLite WAL 체크포인트와 파일 디스크립터 누수를 진단할 때',
                'steps': [
                    'PRAGMA wal_checkpoint(PASSIVE) 실행 후 프레임 카운트를 수집한다.',
                    'lsof 출력에서 열린 sqlite3 핸들 개수가 상한 이내인지 검증한다.',
                ],
                'outcome': '데이터베이스 체크포인트 정상화',
                'conditions': '아키텍처 스토리지 트랜잭션 지연 발생 시에만 적용',
                'counterexamples': '쓰기 락 보유 중에는 TRUNCATE 모드를 강제하지 않음',
            },
            {
                'situation': '아키텍처 모바일 브리지 웹소켓 스트리밍 프레임을 디버깅할 때',
                'steps': [
                    '브리지 루프백 포트에서 핸드셰이크 헤더와 핑 주기를 캡처한다.',
                    '재연결 백오프 타이머와 이벤트 시퀀스 번호 누락 여부를 대조한다.',
                ],
                'outcome': '웹소켓 스트리밍 연결 복구 완료',
                'conditions': '아키텍처 모바일 클라이언트 재연결 지연 시에만 적용',
                'counterexamples': '인증 토큰 만료 상태에서는 자동 재연결을 시도하지 않음',
            },
        ]
        for idx, card in enumerate(distinct_cards, 1):
            self.learn_experience(
                item_id=f'experience:arch-{idx}',
                card=card,
                ref_id=f'rep-prog-{idx}',
                quote=f'아키텍처 점검 보고서 {idx}단계',
                operation=f'op-prog-skill-{idx}',
            )

        ret = self.store.memory('task-one', '아키텍처 점검 서브시스템', limit=6)['retrieval']
        self.assertEqual(2, len(ret['primaryItems']))
        self.assertGreaterEqual(len(ret['indexItems']), 3)
        self.assertTrue(all(x['disclosureStage'] == 'index' and 'read --id' in x['readCommand'] for x in ret['indexItems']))
        self.assertEqual(2, len(ret['primarySkills']))
        self.assertEqual(1, len(ret['indexSkills']))
        self.assertEqual('index', ret['indexSkills'][0]['disclosureStage'])

        aug = self.service.augment_turn(OWNER, '아키텍처 점검 서브시스템 알려줘', '아키텍처 점검 서브시스템 알려줘', 'req-prog-aug')
        self.assertIn('추가 연관 목차(2단계 점진적 노출 · 필요 시 read/skill 조회):', aug)
        # Ensure strip_memory_for_display and _restore_memory_prompt still work 100%
        self.assertEqual('아키텍처 점검 서브시스템 알려줘', self.service.strip_memory_for_display(aug))

    def test_v32_bidirectional_wiki_links_and_one_hop_associative_recall(self):
        # Record a unique source text and create two linked items where only the first has keyword '제피로스'
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-link-seed', 'report', 'rep-link-seed', 'assert',
                '제피로스 프로토콜 활성화 시 sentinel_bridge.py 상태 코드와 소켓 핸드셰이크를 검증한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=200,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-assoc-links',
            [
                {
                    'id': 'fact:zephyros-seed',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '제피로스 프로토콜 활성화 시 [[case:socket-handshake-trap]] 주의보를 함께 점검한다.',
                    'links': ['case:socket-handshake-trap'],
                    'entities': ['sentinel_bridge.py'],
                    'sourceRefs': [{'itemId': 'rep-link-seed', 'quote': '제피로스 프로토콜 활성화 시 sentinel_bridge.py'}],
                },
                {
                    'id': 'case:socket-handshake-trap',
                    'kind': 'case',
                    'op': 'assert',
                    'certainty': 'derived',
                    'entities': ['sentinel_bridge.py'],
                    'sourceRefs': [{'itemId': 'rep-link-seed', 'quote': '소켓 핸드셰이크를 검증한다.'}],
                    'case': {
                        'title': '소켓 핸드셰이크 타임아웃 누락 함정',
                        'situation': 'sentinel_bridge.py 소켓 연결 초기화 시 타임아웃 미지정 상황',
                        'symptom': '비정상 피어 응답 대기 중 워커 스레드가 무기한 블로킹됨',
                        'avoidance': ['소켓 연결 시 timeout=5.0 초를 명시적으로 설정한다.'],
                        'conditions': 'sentinel_bridge.py 소켓 핸드셰이크 초기화 시에만 적용',
                        'severity': 'critical',
                    },
                },
            ],
        )

        # Query ONLY for '제피로스' (which does NOT appear in case:socket-handshake-trap text!)
        ret = self.store.memory('task-one', '제피로스')['retrieval']
        recalled_ids = [r['item_id'] for r in ret['items']]
        self.assertIn('fact:zephyros-seed', recalled_ids)
        self.assertIn('case:socket-handshake-trap', recalled_ids)
        trap_item = next(r for r in ret['items'] if r['item_id'] == 'case:socket-handshake-trap')
        self.assertGreater(trap_item['signals'].get('associative', 0.0), 0.0)
        self.assertEqual('fact:zephyros-seed', trap_item['associativeFrom']['fromId'])
        self.assertIn('[[case:socket-handshake-trap]]', next(r for r in ret['items'] if r['item_id'] == 'fact:zephyros-seed')['links'])
        self.assertIn('[[fact:zephyros-seed]]', trap_item['links'])

        # Check memory_links API and CLI
        api_links = self.service.action({'action': 'memory_links', 'taskId': 'task-one', 'nodeId': 'fact:zephyros-seed'})
        self.assertTrue(api_links['ok'])
        self.assertTrue(any(e['targetId'] == 'case:socket-handshake-trap' for e in api_links['links']))

    def test_v33_standalone_mcp_server_and_cli_bootstrap(self):
        import prism_memory_mcp as mcp_mod
        with tempfile.TemporaryDirectory() as stand_dir:
            srv = mcp_mod.PrismMemoryMCPServer(stand_dir, default_task='acme/standalone-task', default_session='agent-zero')
            init_resp = srv.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
            self.assertEqual('prism-memory-mcp', init_resp['result']['serverInfo']['name'])

            tools_resp = srv.handle_rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            tool_names = {t['name'] for t in tools_resp['result']['tools']}
            self.assertGreaterEqual(len(tool_names), 10)
            self.assertIn('prism_memory_capture', tool_names)
            self.assertIn('prism_memory_inspector', tool_names)
            self.assertIn('prism_memory_export_rules', tool_names)

            cap_resp = srv.handle_rpc({
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {
                    'name': 'prism_memory_capture',
                    'arguments': {
                        'text': '1. sqlite3 WAL 체크포인트 상태를 먼저 확인한다.\n2. 트랜잭션 락 해제 후 마이그레이션을 적용한다.',
                        'command': 'migrate_db.py',
                    },
                },
            })
            self.assertFalse(cap_resp['result']['isError'])
            cap_data = cap_resp['result']['structuredContent']
            self.assertEqual('auto-draft', cap_data['captureMode'])
            self.assertEqual('experience', cap_data['detectedKind'])
            self.assertEqual(1, len(cap_data['skills']))

            res_list = srv.handle_rpc({'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list', 'params': {}})
            uris = [r['uri'] for r in res_list['result']['resources']]
            self.assertIn('prism-memory://acme/standalone-task/inspector', uris)

            res_read = srv.handle_rpc({
                'jsonrpc': '2.0',
                'id': 5,
                'method': 'resources/read',
                'params': {'uri': 'prism-memory://acme/standalone-task/inspector'},
            })
            insp_obj = json.loads(res_read['result']['contents'][0]['text'])
            self.assertEqual('acme/standalone-task', insp_obj['taskId'])
            self.assertGreaterEqual(insp_obj['summary']['skillNodes'], 1)

    def test_v33_auto_capture_two_tier_gear_and_core_fact_protection(self):
        # 1. Error log auto-detected as 'case' with auto-draft provenance
        res_case = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-cap-err-1',
            'text': 'Traceback: ConnectionResetError in bridge_socket.py when worker pool exceeds 32 sockets.',
            'command': 'bridge_socket.py',
        })
        self.assertTrue(res_case['ok'])
        self.assertEqual('auto-draft', res_case['captureMode'])
        self.assertEqual('case', res_case['detectedKind'])
        self.assertTrue(res_case['sourceObservationId'].startswith('report:capture-'))

        # Replay with same operationId is idempotent
        replay_case = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-cap-err-1',
            'text': 'Traceback: ConnectionResetError in bridge_socket.py when worker pool exceeds 32 sockets.',
            'command': 'bridge_socket.py',
        })
        self.assertTrue(replay_case['replayed'])

        # 2. Attempting to auto-capture a 'constraint' or 'fact' is safely downgraded to 'hypothesis'
        res_downgrade = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-cap-downgrade-1',
            'kind': 'constraint',
            'text': '임의의 미검증 자동 수집 문장이 핵심 제약을 덮어쓰려 시도함',
        })
        self.assertTrue(res_downgrade['ok'])
        self.assertEqual('hypothesis', res_downgrade['detectedKind'])
        self.assertEqual('constraint', res_downgrade['downgradedFrom'])
        mem_snap = self.store.memory('task-one', include_retrieval=False)
        self.assertFalse(any('임의의 미검증 자동 수집' in r['content'] for r in mem_snap['current']))
        self.assertTrue(any('임의의 미검증 자동 수집' in r['content'] for r in mem_snap['hypotheses']))

    def test_v33_quantitative_benchmark_token_savings_and_retrieval_scorecard(self):
        self.learn_experience()
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-bench-long', 'report', 'rep-bench-long', 'assert',
                ('장문 아카이브 로그 데이터 블록 ' * 180) + ' 핵심키워드_오메가99 검증 완료.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=210,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-bench-timed',
            [{
                'id': 'hypothesis:bench-timed',
                'kind': 'hypothesis',
                'op': 'assert',
                'certainty': 'derived',
                'content': '앞으로 2시간 동안 핵심키워드_오메가99 캐시 모드를 유지한다.',
                'foresightBaseTime': '2026-10-20T00:00:00+09:00',
                'sourceRefs': [{'itemId': 'rep-bench-long', 'quote': '핵심키워드_오메가99 검증 완료.'}],
            }],
        )
        bench = self.service.action({'action': 'memory_benchmark', 'taskId': 'task-one', 'budget': 3000})
        self.assertTrue(bench['ok'])
        self.assertIn(bench['version'], ('7.0', '8.0'))
        self.assertGreater(bench['tokenEconomy']['rawCorpusChars'], bench['tokenEconomy']['deliveredPromptChars'])
        self.assertGreater(bench['tokenEconomy']['tokenSavingsVsFullHistoryPct'], 20.0)
        self.assertGreaterEqual(bench['retrievalQuality']['recallAt3'], 0.8)
        self.assertEqual(1.0, bench['retrievalQuality']['temporalGuardAccuracy'])
        self.assertEqual(100.0, bench['governance']['provenanceVerifiedPct'])
        self.assertTrue(bench['governance']['zeroExternalApiCost'])

    def test_v33_visual_inspector_graph_mermaid_and_health_alerts(self):
        ref_file = self.root / 'watched_module.py'
        ref_file.write_text('print("v1")\n', encoding='utf-8')
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-insp', 'report', 'rep-insp', 'assert',
                f'{ref_file} 모듈을 점검하고 재시작 순서를 검증했다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=220,
            )
        self.learn_experience(
            item_id='experience:watched-skill',
            card={
                'situation': f'{ref_file} 변경 후 모듈을 재적재할 때',
                'steps': [
                    f'{ref_file} 문법 검사를 먼저 수행한다.',
                    '프로세스 헬스체크 응답을 확인하고 전환한다.',
                ],
                'outcome': '무중단 모듈 재적재 성공',
                'conditions': f'{ref_file} 코드가 수정된 경우에만 적용',
                'counterexamples': '외부 설정 파일만 바뀐 경우에는 재적재하지 않음',
            },
            ref_id='rep-insp',
            quote=f'{ref_file} 모듈을 점검하고',
            operation='op-insp-skill',
        )
        # Modify the referenced file to trigger a staleness alert in inspector
        ref_file.write_text('print("v2-modified")\n', encoding='utf-8')

        insp = self.service.action({'action': 'memory_inspector', 'taskId': 'task-one', 'budget': 4000})
        self.assertTrue(insp['ok'])
        self.assertGreaterEqual(insp['summary']['skillNodes'], 1)
        self.assertIn('flowchart LR', insp['graph']['mermaid'])
        self.assertTrue(any(a['kind'] == 'stale_skill' for a in insp['alerts']))
        self.assertIn('budgetUsedPct', insp['budgetGauge'])

    def test_v33_multi_agent_exporter_and_multilingual_temporal_bounds(self):
        # Test English and Japanese temporal bounds inference
        base_iso = '2026-10-20T10:00:00+09:00'
        en_bounds = memory.infer_temporal_bounds('Keep staging freeze until tomorrow for release check.', base_time=base_iso)
        self.assertIsNotNone(en_bounds)
        self.assertIn('until tomorrow', en_bounds['matched'][0].lower())

        en_dur = memory.infer_temporal_bounds('Enable verbose tracing in the next 4 hours only.', base_time=base_iso)
        self.assertIsNotNone(en_dur)
        self.assertAlmostEqual(en_dur['baseTime'] + 4 * 3600, en_dur['validUntil'], places=1)

        ja_bounds = memory.infer_temporal_bounds('2026-10-25までキャッシュ検証モードを維持する', base_time=base_iso)
        self.assertIsNotNone(ja_bounds)
        self.assertIn('2026-10-25まで', ja_bounds['matched'])

        # Test multi-agent exporter with marker preservation and symlink rejection
        self.learn_experience()
        export_dir = self.root / 'exported_rules'
        export_dir.mkdir(parents=True, exist_ok=True)
        claude_md = export_dir / 'CLAUDE.md'
        claude_md.write_text('# Team Custom Header\nKeep existing project rules here.\n', encoding='utf-8')

        exp_res = self.service.action({
            'action': 'memory_export',
            'taskId': 'task-one',
            'targets': ['claude', 'agents', 'cursor', 'windsurf', 'mcp'],
            'targetDir': str(export_dir),
            'writeFiles': True,
            'lang': 'ko',
        })
        self.assertTrue(exp_res['ok'])
        self.assertEqual(5, len(exp_res['writtenFiles']))
        updated_claude = claude_md.read_text(encoding='utf-8')
        self.assertIn('# Team Custom Header', updated_claude)
        self.assertIn('<!-- PRISM_MEMORY_HARNESS_BEGIN -->', updated_claude)
        self.assertIn('skill:web-cutover', updated_claude)

        # Re-exporting in English updates only the marker block without duplicating the custom header
        exp_en = self.service.action({
            'action': 'memory_export',
            'taskId': 'task-one',
            'targets': ['claude'],
            'targetDir': str(export_dir),
            'writeFiles': True,
            'lang': 'en',
        })
        self.assertTrue(exp_en['ok'])
        updated_en = claude_md.read_text(encoding='utf-8')
        self.assertEqual(1, updated_en.count('# Team Custom Header'))
        self.assertIn('### 1. Core Invariants & Decisions', updated_en)

    def test_v40_hot_binary_vector_matrix_and_lru_embedding_cache(self):
        import memory_vectors as vec_mod
        import struct
        q_blob = struct.pack('<4f', 1.0, 0.0, 0.0, 0.0)
        b1 = struct.pack('<4f', 1.0, 0.0, 0.0, 0.0)
        b2 = struct.pack('<4f', 0.0, 1.0, 0.0, 0.0)
        b3 = struct.pack('<4f', 0.5, 0.5, 0.0, 0.0)
        sims = vec_mod.batch_similarity(q_blob, [b1, b2, b3])
        self.assertEqual(3, len(sims))
        self.assertAlmostEqual(1.0, sims[0], places=4)
        self.assertAlmostEqual(0.0, sims[1], places=4)
        self.assertAlmostEqual(0.5, sims[2], places=4)
        st = vec_mod.status()
        self.assertIn('cacheSize', st)
        self.assertIn('cacheHitRate', st)

    def test_v40_session_delta_prompting_multi_turn_compression(self):
        self.learn_experience()
        # Turn 1: full context injection
        t1 = self.service.augment_turn(OWNER, 'server.py 포트 4388 무중단 교체 점검', 'USER_MSG_1', 'req-v40-t1')
        self.assertIn('현재 필수 조건:', t1)
        self.assertNotIn('세션 델타 모드', t1)

        # Turn 2 with same session and unchanged core invariants: delta-only prompt!
        t2 = self.service.augment_turn(OWNER, 'server.py 포트 4388 무중단 교체 점검', 'USER_MSG_2', 'req-v40-t2')
        self.assertIn('세션 델타 모드 · Turn 2', t2)
        self.assertIn('coreUnchanged', t2)
        self.assertIn('세션 내 기전달 유지 항목(본문 중복 생략):', t2)
        self.assertLess(len(t2), len(t1) * 0.65)

        # If core constraint changes, next turn automatically resets to full Turn 1 mode
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'inp-new-const', 'instruction', 'inp-new-const', 'assert',
                '새로운 보안 제약: 외부 웹훅 호출을 전면 차단한다.',
                {'kind': 'observed-turn', 'session': OWNER, 'speaker': 'user'}, event_at=300,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-new-const',
            [{
                'id': 'constraint:block-webhook',
                'kind': 'constraint',
                'op': 'assert',
                'certainty': 'explicit',
                'content': '외부 웹훅 호출을 전면 차단한다.',
                'sourceRefs': [{'itemId': 'inp-new-const', 'quote': '외부 웹훅 호출을 전면 차단한다.'}],
            }],
        )
        t3 = self.service.augment_turn(OWNER, 'server.py 포트 4388 무중단 교체 점검', 'USER_MSG_3', 'req-v40-t3')
        self.assertNotIn('세션 델타 모드', t3)
        self.assertIn('외부 웹훅 호출을 전면 차단한다.', t3)

    def test_v40_query_intent_adaptive_rrf_and_coverage_reranker_recall_at_1(self):
        id_intent = memory.classify_query_intent('memory_intelligence.py v4.0 4388 skill:web-cutover')
        self.assertEqual('identifier', id_intent['intent'])
        self.assertGreater(id_intent['weights']['lexical'], id_intent['weights']['semantic'])

        concept_intent = memory.classify_query_intent('어떻게 기억과 절차 지식을 점진적으로 개선하고 정리할까')
        self.assertEqual('conceptual', concept_intent['intent'])
        self.assertGreater(concept_intent['weights']['semantic'], concept_intent['weights']['lexical'])

        # Coverage reranker ensures exact full-coverage item beats a neighbor with associative boost
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-cov-seed', 'report', 'rep-cov-seed', 'assert',
                '크로노스 스케줄러 데드락 현상은 뮤텍스 락 순서 역전 때문에 발생하며 워커 큐 타임아웃을 유발한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=310,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-cov-rank',
            [
                {
                    'id': 'fact:kronos-deadlock-exact',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '크로노스 스케줄러 데드락 현상은 뮤텍스 락 순서 역전 때문에 발생한다. [[case:kronos-side-case]]',
                    'links': ['case:kronos-side-case'],
                    'sourceRefs': [{'itemId': 'rep-cov-seed', 'quote': '크로노스 스케줄러 데드락 현상은 뮤텍스 락 순서 역전 때문에 발생하며'}],
                },
                {
                    'id': 'case:kronos-side-case',
                    'kind': 'case',
                    'op': 'assert',
                    'certainty': 'derived',
                    'sourceRefs': [{'itemId': 'rep-cov-seed', 'quote': '워커 큐 타임아웃을 유발한다.'}],
                    'case': {
                        'title': '크로노스 워커 큐 타임아웃 주의보',
                        'situation': '크로노스 워커 큐가 포화될 때',
                        'symptom': '워커 큐 타임아웃을 유발한다.',
                        'avoidance': ['워커 큐 크기를 점검한다.'],
                        'conditions': '크로노스 워커 큐 포화 시에만 적용',
                        'severity': 'warning',
                    },
                },
            ],
        )
        ret = self.store.memory('task-one', '크로노스 스케줄러 데드락 뮤텍스 락 순서 역전')['retrieval']
        self.assertEqual('fact:kronos-deadlock-exact', ret['items'][0]['item_id'])
        self.assertGreater(ret['items'][0]['signals'].get('coverage', 0.0), 0.0)

    def test_v40_two_hop_ppr_graph_propagation_and_anti_leapfrog_guard(self):
        # Create a 2-hop chain: node A -> node B -> node C where only node A contains '히페리온코어'
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-ppr-0', 'report', 'rep-ppr-0', 'assert',
                '히페리온코어 엔진 초기화는 게이트웨이 라우터를 호출한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=320,
            )
            ledger.record(
                db, 'task-one', 1, 'rep-ppr-1', 'report', 'rep-ppr-1', 'assert',
                '게이트웨이 라우터는 내부 중계 버스를 통과한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=321,
            )
            ledger.record(
                db, 'task-one', 1, 'rep-ppr-2', 'report', 'rep-ppr-2', 'assert',
                '최종적으로 스토리지 샤드 밸런서를 깨운다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=322,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-ppr-chain',
            [
                {
                    'id': 'fact:ppr-hop0-hyperion',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '히페리온코어 엔진 초기화 단계 기록. [[fact:ppr-hop1-gateway]]',
                    'links': ['fact:ppr-hop1-gateway'],
                    'sourceRefs': [{'itemId': 'rep-ppr-0', 'quote': '히페리온코어 엔진 초기화는'}],
                },
                {
                    'id': 'fact:ppr-hop1-gateway',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '게이트웨이 라우터 연결 단계 기록. [[fact:ppr-hop2-shard]]',
                    'links': ['fact:ppr-hop2-shard'],
                    'sourceRefs': [{'itemId': 'rep-ppr-1', 'quote': '게이트웨이 라우터는 내부 중계 버스를'}],
                },
                {
                    'id': 'fact:ppr-hop2-shard',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '최종적으로 스토리지 샤드 밸런서를 깨운다.',
                    'sourceRefs': [{'itemId': 'rep-ppr-2', 'quote': '최종적으로 스토리지 샤드 밸런서를 깨운다.'}],
                },
            ],
        )
        ret = self.store.memory('task-one', '히페리온코어')['retrieval']
        recalled_ids = [r['item_id'] for r in ret['items']]
        self.assertEqual('fact:ppr-hop0-hyperion', recalled_ids[0])
        self.assertIn('fact:ppr-hop1-gateway', recalled_ids)
        self.assertIn('fact:ppr-hop2-shard', recalled_ids)
        hop2_item = next(r for r in ret['items'] if r['item_id'] == 'fact:ppr-hop2-shard')
        self.assertEqual(2, hop2_item['associativeFrom']['hop'])

    def test_v40_sufficiency_check_and_memscene_clustering(self):
        self.learn_experience()
        scenes_res = self.service.action({'action': 'memory_scenes', 'taskId': 'task-one'})
        self.assertTrue(scenes_res['ok'])
        self.assertGreaterEqual(scenes_res['sceneCount'], 1)
        self.assertIn('theme', scenes_res['scenes'][0])
        self.assertIn('memberCount', scenes_res['scenes'][0])

    def test_v40_contradiction_supersession_resolver_and_box_evolution(self):
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-evolve-1', 'report', 'rep-evolve-1', 'assert',
                '초기 가설: 캐시 만료 주기를 60초로 추정한다. 이후 실측 결과 캐시 만료 주기를 60초 대신 300초로 변경 확정했다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=330,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-hyp-old',
            [{
                'id': 'hypothesis:cache-ttl-60',
                'kind': 'hypothesis',
                'op': 'assert',
                'certainty': 'derived',
                'content': '초기 가설: 캐시 만료 주기를 60초로 추정한다.',
                'entities': ['cache_ttl'],
                'sourceRefs': [{'itemId': 'rep-evolve-1', 'quote': '초기 가설: 캐시 만료 주기를 60초로 추정한다.'}],
            }],
        )
        # Now learn a verified fact on the same entity with a contradiction/replacement cue and link to another item
        rev2 = self.store.get('task-one')['revision']
        learn_res = memory.learn(
            self.store, 'task-one', OWNER, rev2, 'op-fact-new-ttl',
            [{
                'id': 'fact:cache-ttl-300',
                'kind': 'fact',
                'op': 'assert',
                'certainty': 'derived',
                'content': '이후 실측 결과 캐시 만료 주기를 60초 대신 300초로 변경 확정했다.',
                'entities': ['cache_ttl'],
                'links': ['experience:web-cutover'],
                'sourceRefs': [{'itemId': 'rep-evolve-1', 'quote': '이후 실측 결과 캐시 만료 주기를 60초 대신 300초로 변경 확정했다.'}],
            }],
        )
        self.assertIn('hypothesis:cache-ttl-60', learn_res['supersededItems'])
        # Verify the old hypothesis is no longer active
        snap = self.store.memory('task-one', include_retrieval=False)
        self.assertFalse(any(h['item_id'] == 'hypothesis:cache-ttl-60' for h in snap['hypotheses']))

    def test_v50_grounded_abstention_gate_and_recognition_filter(self):
        self.learn_experience()
        # Out-of-domain / unanswerable query should trigger abstention with 0 false-positive primary items
        unans_ret = self.store.memory('task-one', '안드로메다 은하계 초신성 중력파 스펙트럼 양자중력 방정식 998877')['retrieval']
        self.assertTrue(unans_ret['abstained'])
        self.assertEqual('no_grounded_evidence', unans_ret['abstentionReason'])
        self.assertEqual([], unans_ret['primaryItems'])
        self.assertEqual([], unans_ret['primarySkills'])
        self.assertEqual([], unans_ret['cases'])

        # Answerable query should NOT abstain and should pass Recognition Filter
        ans_ret = self.store.memory('task-one', 'server.py 포트 4388 무중단 교체')['retrieval']
        self.assertFalse(ans_ret['abstained'])
        self.assertGreater(ans_ret['confidenceScore'], 0.05)
        self.assertGreaterEqual(len(ans_ret['primaryItems']) + len(ans_ret['primarySkills']), 1)

    def test_v50_synapse_lateral_inhibition_diversity(self):
        with self.store.db(True) as db:
            for idx in range(1, 4):
                ledger.record(
                    db, 'task-one', 1, f'rep-lat-{idx}', 'report', f'rep-lat-{idx}', 'assert',
                    f'제논클러스터 샤드 라우팅 테이블 복제 상태 점검 수칙 {idx}번: 샤드 라우팅 테이블 복제 지연 시 헬스체크를 확인한다.',
                    {'kind': 'observed-response', 'session': OWNER}, event_at=400 + idx,
                )
            ledger.record(
                db, 'task-one', 1, 'rep-lat-distinct', 'report', 'rep-lat-distinct', 'assert',
                '제논클러스터 TLS 인증서 로테이션 및 mTLS 키교환 만료일 검증 프로토콜을 확인한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=405,
            )
        ret = self.store.memory('task-one', '제논클러스터 점검', limit=6)['retrieval']
        self.assertFalse(ret['abstained'])
        self.assertGreaterEqual(ret.get('lateralInhibitionsCount', 0), 1)
        self.assertGreater(ret.get('stage1DiversityScore', 0.0), 0.35)
        # Ensure total items are still intact while primaryItems are diversified
        self.assertGreaterEqual(len(ret['items']), 3)
        self.assertEqual(2, len(ret['primaryItems']))

    def test_v50_amac_admission_control_and_actr_lifecycle(self):
        # 1. Low-utility boilerplate greeting is rejected by A-MAC Admission Control
        rej_noise = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-amac-noise-1',
            'text': '네 알겠습니다 감사합니다 확인했습니다',
        })
        self.assertTrue(rej_noise['ok'])
        self.assertFalse(rej_noise['admitted'])
        self.assertIn(rej_noise['admission']['reason'], ('low_information_noise', 'below_amac_threshold'))

        # 2. High-utility procedural experience is admitted, then near-duplicate is rejected
        adm_first = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-amac-good-1',
            'text': '1. redis_sentinel.py 페일오버 전 마스터 쿼럼 숫자를 확인한다.\n2. 슬레이브 복제 오프셋 동기화 후 스위칭한다.',
            'command': 'redis_sentinel.py',
        })
        self.assertTrue(adm_first['ok'])
        self.assertTrue(adm_first['admitted'])

        rej_dup = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-amac-dup-1',
            'text': '1. redis_sentinel.py 페일오버 전 마스터 쿼럼 숫자를 확인한다.\n2. 슬레이브 복제 오프셋 동기화 후 스위칭한다.',
            'command': 'redis_sentinel.py',
        })
        self.assertTrue(rej_dup['ok'])
        self.assertFalse(rej_dup['admitted'])
        self.assertIn(rej_dup['admission']['reason'], ('redundant_duplicate', 'duplicate_or_low_novelty'))

        # 3. ACT-R base-level activation increases with usage/helpful feedback
        act_low = skills_ledger.compute_actr_activation(1000.0, 1000.0, 0, 0, now=2000.0)
        act_high = skills_ledger.compute_actr_activation(1000.0, 1950.0, 5, 4, now=2000.0)
        self.assertGreater(act_high, act_low)

    def test_v50_ast_symbol_anchor_guard_and_git_branch(self):
        mod_file = self.root / 'symbol_guarded.py'
        mod_file.write_text(
            'def target_alpha(x):\n    return x + 1\n\ndef unrelated_beta(y):\n    return y * 2\n',
            encoding='utf-8',
        )
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-sym-1', 'report', 'rep-sym-1', 'assert',
                f'{mod_file}::target_alpha 함수의 반환값 경계 조건을 검증했다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=410,
            )
        self.learn_experience(
            item_id='experience:sym-alpha-skill',
            card={
                'situation': f'{mod_file}::target_alpha 함수를 호출할 때',
                'steps': [
                    f'{mod_file}::target_alpha 입력 타입이 정수인지 확인한다.',
                    '반환값이 양수인지 대조한다.',
                ],
                'outcome': 'target_alpha 검증 완료',
                'conditions': f'{mod_file}::target_alpha 호출 시에만 적용',
                'counterexamples': 'unrelated_beta 호출 시에는 무관함',
            },
            ref_id='rep-sym-1',
            quote=f'{mod_file}::target_alpha 함수의 반환값',
            operation='op-sym-alpha',
        )
        # Modify ONLY unrelated_beta in symbol_guarded.py -> target_alpha AST hash is UNCHANGED!
        mod_file.write_text(
            'def target_alpha(x):\n    return x + 1\n\ndef unrelated_beta(y):\n    return y * 999 + 42\n',
            encoding='utf-8',
        )
        skills_list = self.store.skills('task-one')['skills']
        sym_skill = next(s for s in skills_list if 'target_alpha' in s['title'])
        self.assertFalse(sym_skill['stale'])
        self.assertGreaterEqual(len(sym_skill['staleness'].get('symbolAnchoredSaved', [])), 1)

        # Now modify target_alpha body -> AST hash changes -> skill becomes stale!
        mod_file.write_text(
            'def target_alpha(x):\n    return x + 500\n\ndef unrelated_beta(y):\n    return y * 999 + 42\n',
            encoding='utf-8',
        )
        skills_list_after = self.store.skills('task-one')['skills']
        sym_skill_after = next(s for s in skills_list_after if 'target_alpha' in s['title'])
        self.assertTrue(sym_skill_after['stale'])
        self.assertTrue(any('referenced_symbol_changed:' in r for r in sym_skill_after['staleness']['reasons']))

    def test_v50_bidirectional_markdown_coedit_sync(self):
        self.learn_experience()
        skill_before = skills_ledger.read_skill(self.store, 'task-one', 'skill:web-cutover')
        md_path = Path(skill_before['markdownPath'])
        md_text = md_path.read_text(encoding='utf-8')
        # Simulate user editing SKILL.md directly in their IDE
        edited_md = md_text.replace(
            '운영 API와 SQLite 원장을 다시 읽어 스냅샷과 일치하는지 검증한다.',
            '운영 API와 SQLite 원장 해시 및 4388 소켓 응답을 즉시 대조한다.',
        )
        self.assertNotEqual(md_text, edited_md)
        md_path.write_text(edited_md, encoding='utf-8')

        # 1) Default sync without explicitUser keeps evidenceState at reported and marks compatible-unverified
        sync_res = self.service.action({'action': 'memory_sync_markdown', 'taskId': 'task-one', 'session': OWNER})
        self.assertTrue(sync_res['ok'])
        self.assertEqual(1, sync_res['skillSync']['syncedCount'])
        skill_after = skills_ledger.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertIn('4388 소켓 응답을 즉시 대조한다', ' '.join(skill_after['steps']))
        self.assertEqual('reported', skill_after['evidenceState'])
        self.assertEqual('compatible-unverified', skill_after['source'].get('verificationStatus'))

        # 2) Explicit human co-edit confirmation (`explicitUser=True`) promotes to user-confirmed
        md_text_v2 = md_path.read_text(encoding='utf-8')
        md_path.write_text(
            md_text_v2.replace('4388 소켓 응답을 즉시 대조한다.', '4388 소켓 응답과 프로세스 PID를 즉시 대조한다.'),
            encoding='utf-8',
        )
        sync_res_explicit = self.service.action({
            'action': 'memory_sync_markdown',
            'taskId': 'task-one',
            'session': OWNER,
            'explicitUser': True,
        })
        self.assertTrue(sync_res_explicit['ok'])
        skill_confirmed = skills_ledger.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertEqual('user-confirmed', skill_confirmed['evidenceState'])
        self.assertEqual('verified', skill_confirmed['source'].get('verificationStatus'))

    def test_v50_memtree_hierarchy_and_causal_triples(self):
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-causal-1', 'report', 'rep-causal-1', 'assert',
                '원장 백업 검증은 스키마 마이그레이션에 의존하며 장애 위험을 완화한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=420,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-causal-1',
            [
                {
                    'id': 'fact:schema-mig-base',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '스키마 마이그레이션 기준 절차 정의.',
                    'sourceRefs': [{'itemId': 'rep-causal-1', 'quote': '스키마 마이그레이션에 의존하며'}],
                },
                {
                    'id': 'fact:backup-verify',
                    'kind': 'fact',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': '원장 백업 검증은 [[depends_on:fact:schema-mig-base]] 이며 [[mitigates:case:cutover-trap]] 한다.',
                    'sourceRefs': [{'itemId': 'rep-causal-1', 'quote': '원장 백업 검증은 스키마 마이그레이션에 의존하며'}],
                },
            ],
        )
        scenes_res = self.service.action({'action': 'memory_scenes', 'taskId': 'task-one'})
        self.assertTrue(scenes_res['ok'])
        mem_tree = scenes_res['memTree']
        self.assertEqual(4, mem_tree['depth'])
        self.assertGreaterEqual(mem_tree['pillarCount'], 1)
        rels = {t['relation'] for t in mem_tree['causalTriples']}
        self.assertIn('depends_on', rels)
        self.assertIn('mitigates', rels)

    def test_v50_lossless_memory_consolidation_zero_drift(self):
        with self.store.db(True) as db:
            for idx in range(1, 4):
                ledger.record(
                    db, 'task-one', 1, f'rep-cons-{idx}', 'report', f'rep-cons-{idx}', 'assert',
                    f'플라즈마캐시 워커 풀 타임아웃 튜닝 실험 {idx}차: 플라즈마캐시 워커 풀 타임아웃 값을 15초로 조정해 연결 지연을 줄인다.',
                    {'kind': 'observed-response', 'session': OWNER}, event_at=430 + idx,
                )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-cons-seed',
            [
                {
                    'id': f'hypothesis:plasma-timeout-{idx}',
                    'kind': 'hypothesis',
                    'op': 'assert',
                    'certainty': 'derived',
                    'content': f'플라즈마캐시 워커 풀 타임아웃 튜닝 실험 {idx}차: 플라즈마캐시 워커 풀 타임아웃 값을 15초로 조정해 연결 지연을 줄인다.',
                    'entities': ['plasma_cache'],
                    'sourceRefs': [{'itemId': f'rep-cons-{idx}', 'quote': f'플라즈마캐시 워커 풀 타임아웃 튜닝 실험 {idx}차'}],
                }
                for idx in range(1, 4)
            ],
        )
        before_snap = self.store.memory('task-one', include_retrieval=False)
        core_before = [(c['item_id'], c['content']) for c in before_snap['current']]

        cons_res = self.service.action({
            'action': 'memory_consolidate',
            'taskId': 'task-one',
            'session': OWNER,
            'similarityThreshold': 0.25,
            'dryRun': False,
        })
        self.assertTrue(cons_res['ok'])
        self.assertEqual(0.0, cons_res['identityDrift'])
        self.assertGreaterEqual(cons_res['consolidatedCount'], 1)
        self.assertGreaterEqual(cons_res['supersededHypothesesCount'], 2)

        after_snap = self.store.memory('task-one', include_retrieval=False)
        core_after = [(c['item_id'], c['content']) for c in after_snap['current']]
        # Core constraints/goals/decisions must remain 100% identical (0% identity drift)
        for cid, ctext in core_before:
            if cid.startswith(('goal:', 'constraint:', 'decision:')):
                self.assertIn((cid, ctext), core_after)

    def test_v84_js_ts_template_regex_jsx_and_hoc_signatures(self):
        from prism_memory.code_anchors import extract_file_symbol_signatures
        from prism_memory.audit import audit_repository

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) Template string & regex literal interior declarations must NOT be extracted as symbols
            p_tmpl = root / 'tmpl.ts'
            p_tmpl.write_text('const text = `\nexport function Ghost(x) {}\n`;\nexport const real = x => x;\n', encoding='utf-8')
            sig_tmpl = extract_file_symbol_signatures(p_tmpl)
            self.assertTrue(sig_tmpl['__syntax_ok__'])
            self.assertNotIn('Ghost', sig_tmpl['symbols'])
            self.assertIn('real', sig_tmpl['symbols'])

            # 2) Unclosed regex literal or empty assignment `const x = ;` must flag __syntax_ok__ = False
            p_unclosed = root / 'unclosed.ts'
            p_unclosed.write_text('export const r = /unterminated', encoding='utf-8')
            self.assertFalse(extract_file_symbol_signatures(p_unclosed)['__syntax_ok__'])

            p_empty_assign = root / 'empty_assign.ts'
            p_empty_assign.write_text('export const real = () => { const x = ; return x; };', encoding='utf-8')
            self.assertFalse(extract_file_symbol_signatures(p_empty_assign)['__syntax_ok__'])

            # 3) Nested HOCs (`React.memo(React.forwardRef(...))`), generic HOC (`withAuth(...)`), and JSX closing tags (`</div>`)
            p_hoc = root / 'hoc.tsx'
            p_hoc.write_text(
                'export const App = React.memo(React.forwardRef((props, ref) => <div className="x">ok</div>));\n'
                'export const Guarded = withAuth((props) => <span>hi</span>);\n',
                encoding='utf-8',
            )
            sig_hoc = extract_file_symbol_signatures(p_hoc)
            self.assertTrue(sig_hoc['__syntax_ok__'])
            self.assertIn('App', sig_hoc['symbols'])
            self.assertIn('Guarded', sig_hoc['symbols'])

            (root / 'AGENTS.md').write_text('Use `tmpl.ts::Ghost`.\n', encoding='utf-8')
            aud = audit_repository(root)
            self.assertFalse(aud['ok'])
            self.assertEqual(1, aud['countsByRule'].get('missing_symbol', 0))

    def test_v84_audit_strict_workspace_cd_and_tutorial_guards(self):
        from prism_memory.audit import audit_repository

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) Wrong package script after `cd packages/app` must NOT fall back to root package.json
            w_pkg = root / 'wrong_pkg'
            (w_pkg / 'packages' / 'app').mkdir(parents=True)
            (w_pkg / 'package.json').write_text('{"scripts":{"build":"echo root"}}', encoding='utf-8')
            (w_pkg / 'packages' / 'app' / 'package.json').write_text('{"scripts":{}}', encoding='utf-8')
            (w_pkg / 'AGENTS.md').write_text('```sh\ncd packages/app\nnpm run build\n```\n', encoding='utf-8')
            res_wp = audit_repository(w_pkg)
            self.assertFalse(res_wp['ok'])
            self.assertEqual(1, res_wp['countsByRule'].get('missing_script', 0))

            # 2) Wrong path after `cd packages/b` must NOT suffix-match `packages/a/src/main.ts`
            w_path = root / 'wrong_path'
            (w_path / 'packages' / 'a' / 'src').mkdir(parents=True)
            (w_path / 'packages' / 'b').mkdir(parents=True)
            (w_path / 'packages' / 'a' / 'src' / 'main.ts').write_text('export const main = 1;\n', encoding='utf-8')
            (w_path / 'packages' / 'b' / 'package.json').write_text('{}', encoding='utf-8')
            (w_path / 'AGENTS.md').write_text('```sh\ncd packages/b\nnode src/main.ts\n```\n', encoding='utf-8')
            res_wpath = audit_repository(w_path)
            self.assertFalse(res_wpath['ok'])
            self.assertEqual(1, res_wpath['countsByRule'].get('missing_path', 0))

            # 3) `Run \`gone.py\`` flags missing_path while `Create a file named \`server.py\`. Then run \`python server.py\`` passes
            m_bare = root / 'missing_bare'
            m_bare.mkdir()
            (m_bare / 'AGENTS.md').write_text('Run `gone.py`.\n', encoding='utf-8')
            res_bare = audit_repository(m_bare)
            self.assertFalse(res_bare['ok'])
            self.assertEqual(1, res_bare['countsByRule'].get('missing_path', 0))

            tut = root / 'tutorial'
            tut.mkdir()
            (tut / 'README.md').write_text('Create a file named `server.py`. Then run `python server.py`.\n', encoding='utf-8')
            res_tut = audit_repository(tut)
            self.assertTrue(res_tut['ok'])

    def test_v60_in_memory_inverted_index_and_link_cache_sub5ms(self):
        self.learn_experience()
        ret1 = self.store.memory('task-one', 'server.py 포트 4388 무중단 교체')['retrieval']
        self.assertFalse(ret1['abstained'])
        self.assertIn('task-one', memory._TASK_INVERTED_INDEX)
        self.assertIn('task-one', skills_ledger._TASK_LINK_CACHE)
        cached_rev = memory._TASK_INVERTED_INDEX['task-one']['revision']
        self.assertGreaterEqual(cached_rev, 1)

        # Adding a new ledger record bumps revision and refreshes the inverted index automatically
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-inv-idx-1', 'report', 'rep-inv-idx-1', 'assert',
                '벨로시티역색인 토큰 포스팅 리스트 캐시 무결성 확인 완료.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=500,
            )
        ret2 = self.store.memory('task-one', '벨로시티역색인 토큰 포스팅')['retrieval']
        self.assertGreater(memory._TASK_INVERTED_INDEX['task-one']['revision'], cached_rev)
        self.assertTrue(any(r['item_id'] == 'rep-inv-idx-1' for r in ret2['items']))

    def test_v60_prospective_memory_intention_store_and_trigger(self):
        # Capture a conditional future intention (Prospective Memory cue)
        cap_res = self.service.action({
            'action': 'memory_capture',
            'taskId': 'task-one',
            'session': OWNER,
            'operationId': 'op-v60-prosp-1',
            'text': '다음에 server.py 배포할 때 반드시 send_ledger 해시와 input_leases 만료 여부를 먼저 확인할 것.',
            'command': 'server.py',
        })
        self.assertTrue(cap_res['ok'])
        self.assertTrue(cap_res['admitted'])
        self.assertEqual('prospective', cap_res['detectedKind'])
        self.assertIsNotNone(cap_res.get('prospectiveCue'))
        self.assertIn('server.py', cap_res['prospectiveCue']['condition'])

        # Now query with matching trigger context ("server.py 배포")
        ret = self.store.memory('task-one', 'server.py 배포 준비 점검')['retrieval']
        self.assertGreaterEqual(len(ret.get('prospectiveTriggers', [])), 1)
        top_trig = ret['prospectiveTriggers'][0]
        self.assertGreaterEqual(top_trig['triggerScore'], 0.30)
        self.assertIn('send_ledger', top_trig['action'])

        aug = self.service.augment_turn(OWNER, 'server.py 배포 진행해줘', 'server.py 배포 진행해줘', 'req-v60-prosp')
        self.assertIn('미래 의도 트리거(Prospective Memory', aug)

    def test_v60_decor_multi_aspect_query_decomposition_and_slot_quota(self):
        with self.store.db(True) as db:
            for idx in range(1, 4):
                ledger.record(
                    db, 'task-one', 1, f'rep-decor-a-{idx}', 'report', f'rep-decor-a-{idx}', 'assert',
                    f'오리온뮤텍스 데드락 락순서 분석 보고서 {idx}호: 오리온뮤텍스 락 경합을 해소한다.',
                    {'kind': 'observed-response', 'session': OWNER}, event_at=510 + idx,
                )
            ledger.record(
                db, 'task-one', 1, 'rep-decor-b-1', 'report', 'rep-decor-b-1', 'assert',
                '네뷸라워커 타임아웃 임계치 설정 가이드: 네뷸라워커 소켓 타임아웃을 15초로 설정한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=515,
            )
        compound_q = '오리온뮤텍스 데드락 락순서 분석 그리고 네뷸라워커 타임아웃 임계치 설정 알려줘'
        facets = memory.decompose_query_facets(compound_q)
        self.assertGreaterEqual(len(facets), 2)

        ret = self.store.memory('task-one', compound_q, limit=6)['retrieval']
        self.assertGreaterEqual(len(ret.get('queryFacets', [])), 2)
        prim_texts = ' '.join(x['excerpt'] for x in ret['primaryItems'])
        self.assertIn('오리온뮤텍스', prim_texts)
        self.assertIn('네뷸라워커', prim_texts)

    def test_v60_trajectory_informed_implicit_credit_assignment(self):
        self.learn_experience()
        # Turn 1: user asks about server.py 4388 cutover -> skill:web-cutover is injected in Stage-1
        self.service.augment_turn(OWNER, 'server.py 4388 무중단 교체 절차 알려줘', 'MSG_T1', 'req-traj-t1')
        skill_t1 = skills_ledger.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertEqual('reported', skill_t1['evidenceState'])

        # Turn 2: user follows up with positive adoption/continuation on the same topic
        self.service.augment_turn(OWNER, '좋아 server.py 4388 교체 정상 작동 확인했어 다음 단계 진행하자', 'MSG_T2', 'req-traj-t2')
        skill_t2 = skills_ledger.read_skill(self.store, 'task-one', 'skill:web-cutover')
        self.assertGreaterEqual(skill_t2['useCount'], 1)
        self.assertEqual('trial-used', skill_t2['evidenceState'])

        # Turn 3: if user reports an error/failure on the same topic, trajectoryWarning is surfaced without overriding user-confirmed
        t3_aug = self.service.augment_turn(OWNER, 'server.py 4388 교체 후 Exception 에러 실패 발생했어 다시 봐줘', 'MSG_T3', 'req-traj-t3')
        self.assertIn('이전 턴 궤적 주의보', t3_aug)

    def test_v60_active_preflight_guardrail_and_constraint_verifier(self):
        self.learn_experience()
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'inp-pf-const', 'instruction', 'inp-pf-const', 'assert',
                '절대 ~/.codex/memories 개인 기억 폴더를 직접 수정하지 마라.',
                {'kind': 'observed-turn', 'session': OWNER, 'speaker': 'user'}, event_at=520,
            )
            ledger.record(
                db, 'task-one', 1, 'rep-pf-case', 'report', 'rep-pf-case', 'assert',
                'server.py 교체 시 input_leases 미확인 상태에서 재시작하면 스트리밍 세션이 유실됨.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=521,
            )
        rev = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev, 'op-pf-setup',
            [
                {
                    'id': 'constraint:no-codex-mem-edit',
                    'kind': 'constraint',
                    'op': 'assert',
                    'certainty': 'explicit',
                    'content': '절대 ~/.codex/memories 개인 기억 폴더를 직접 수정하지 마라.',
                    'sourceRefs': [{'itemId': 'inp-pf-const', 'quote': '절대 ~/.codex/memories 개인 기억 폴더를 직접 수정하지 마라.'}],
                },
                {
                    'id': 'case:server-lease-loss',
                    'kind': 'case',
                    'op': 'assert',
                    'certainty': 'derived',
                    'entities': ['server.py'],
                    'sourceRefs': [{'itemId': 'rep-pf-case', 'quote': 'server.py 교체 시 input_leases 미확인 상태에서 재시작하면'}],
                    'case': {
                        'title': 'server.py 재시작 시 input_leases 유실 주의보',
                        'situation': 'server.py 프로세스를 재시작할 때',
                        'symptom': '스트리밍 세션이 유실됨',
                        'avoidance': ['교체 전 input_leases 활성 토큰을 먼저 확인한다.'],
                        'conditions': 'server.py 배포 시에만 적용',
                        'severity': 'critical',
                    },
                },
            ],
        )
        # 1. Plan touching prohibited ~/.codex/memories -> BLOCK
        pf_block = self.service.action({
            'action': 'memory_preflight',
            'taskId': 'task-one',
            'plan': '~/.codex/memories 개인 기억 폴더 파일을 직접 수정하고 덮어쓴다',
            'files': ['~/.codex/memories/notes.md'],
        })
        self.assertTrue(pf_block['ok'])
        self.assertEqual('BLOCK', pf_block['status'])
        self.assertGreaterEqual(len(pf_block['violatedConstraints']), 1)

        # 2. Plan touching server.py -> WARN with requiredChecklist from case avoidance & skill steps
        pf_warn = self.service.action({
            'action': 'memory_preflight',
            'taskId': 'task-one',
            'plan': 'server.py 프로세스 재시작 배포',
            'files': ['server.py'],
        })
        self.assertTrue(pf_warn['ok'])
        self.assertIn(pf_warn['status'], ('WARN', 'BLOCK'))
        self.assertGreaterEqual(len(pf_warn['triggeredCases']), 1)
        self.assertTrue(any('input_leases' in step for step in pf_warn['requiredChecklist']))

    def test_v60_statemem_evolutionary_lineage_diff_reconstruction(self):
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-lin-1', 'report', 'rep-lin-1', 'assert',
                '초기 설정: 워커 큐 배치 크기를 16으로 가설 설정함. 이후 부하 실험을 통해 워커 큐 배치 크기를 16 대신 64로 변경 확정함.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=530,
            )
        rev1 = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev1, 'op-lin-v1',
            [{
                'id': 'hypothesis:batch-size-16',
                'kind': 'hypothesis',
                'op': 'assert',
                'certainty': 'derived',
                'content': '초기 설정: 워커 큐 배치 크기를 16으로 가설 설정함.',
                'entities': ['worker_batch_size'],
                'sourceRefs': [{'itemId': 'rep-lin-1', 'quote': '초기 설정: 워커 큐 배치 크기를 16으로 가설 설정함.'}],
            }],
        )
        rev2 = self.store.get('task-one')['revision']
        memory.learn(
            self.store, 'task-one', OWNER, rev2, 'op-lin-v2',
            [{
                'id': 'fact:batch-size-64',
                'kind': 'fact',
                'op': 'assert',
                'certainty': 'derived',
                'content': '이후 부하 실험을 통해 워커 큐 배치 크기를 16 대신 64로 변경 확정함.',
                'entities': ['worker_batch_size'],
                'sourceRefs': [{'itemId': 'rep-lin-1', 'quote': '이후 부하 실험을 통해 워커 큐 배치 크기를 16 대신 64로 변경 확정함.'}],
            }],
        )
        q_lineage = '워커 큐 배치 크기가 왜 16에서 64로 바뀌었어 히스토리 변천사'
        intent = memory.classify_query_intent(q_lineage)
        self.assertEqual('lineage', intent['intent'])

        ret = self.store.memory('task-one', q_lineage)['retrieval']
        fact_item = next(r for r in ret['items'] if r['item_id'] == 'fact:batch-size-64')
        self.assertIn('lineageChain', fact_item)
        chain_ids = [step['itemId'] for step in fact_item['lineageChain']]
        self.assertIn('hypothesis:batch-size-16', chain_ids)
        self.assertIn('fact:batch-size-64', chain_ids)

    def test_v60_privacy_safe_cross_project_federated_archetype_transfer(self):
        # Create a skill in task-one (project 'proj-a') with user-specific paths and promote it to feedback-verified
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-fed-1', 'report', 'rep-fed-1', 'assert',
                '/Users/alice/secret_workspace/server_daemon.py 재기동 시 ops@company.io 알림 확인 후 소켓 드레이닝을 먼저 수행한다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=540,
            )
        self.learn_experience(
            item_id='experience:daemon-drain-archetype',
            card={
                'situation': '/Users/alice/secret_workspace/server_daemon.py 프로세스 무중단 소켓 드레이닝을 수행할 때',
                'steps': [
                    '/Users/alice/secret_workspace/server_daemon.py 활성 리스너 큐를 드레이닝 모드로 전환한다.',
                    'ops@company.io 헬스체크 응답이 200인지 확인하고 프로세스를 교체한다.',
                ],
                'outcome': '소켓 드레이닝 무중단 교체 완료',
                'conditions': '데몬 소켓 드레이닝 교체 시에만 적용',
                'counterexamples': '단순 정적 파일 수정 시에는 드레이닝을 생략함',
            },
            ref_id='rep-fed-1',
            quote='/Users/alice/secret_workspace/server_daemon.py 재기동 시',
            operation='op-fed-learn',
        )
        # Promote to user-confirmed via explicit skill management
        skills_list = self.store.skills('task-one')['skills']
        arch_skill = next(s for s in skills_list if '드레이닝' in s['title'])
        rev_now = self.store.get('task-one')['revision']
        self.service.action({
            'action': 'memory_skill_manage',
            'taskId': 'task-one',
            'session': OWNER,
            'revision': rev_now,
            'operationId': 'op-fed-fb-1',
            'op': 'promote',
            'skillId': arch_skill['skillId'],
            'certainty': 'explicit',
            'reason': '다른 프로젝트에도 유용한 표준 절차',
            'sourceRefs': [{'itemId': 'rep-fed-1', 'quote': '/Users/alice/secret_workspace/server_daemon.py 재기동 시'}],
        })

        # Create a brand-new task in a DIFFERENT project ('proj-omega/new-service')
        self.store.create('proj-omega', 'New Omega Service', OWNER, 'Omega goal', 'proj-omega/new-service')
        fed_ret = self.store.memory('proj-omega/new-service', '데몬 소켓 드레이닝 무중단 교체 절차', federated=True)['retrieval']
        self.assertGreaterEqual(len(fed_ret['skills']), 1)
        transferred = next(s for s in fed_ret['skills'] if s['skillId'] == arch_skill['skillId'])
        self.assertEqual('federated-archetype', transferred['sharedMode'])
        # Ensure private paths and emails are sanitized in federated archetype transfer!
        self.assertNotIn('/Users/alice', transferred['title'])
        self.assertNotIn('ops@company.io', ' '.join(transferred['steps']))
        self.assertIn('<project>/', transferred['title'])

    def test_v84_viberescue_string_env_rls_disable_and_written_files_scrub(self):
        from prism_memory.viberescue import build_rescue_bundle

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) String literal `"process.env.REMOVED_API_KEY"` must NOT be flagged as an env reference
            s_dir = root / 'str_ref'
            s_dir.mkdir()
            (s_dir / 'client.js').write_text('const example = "process.env.REMOVED_API_KEY";\n', encoding='utf-8')
            res_str = build_rescue_bundle(s_dir)
            self.assertEqual('STATIC_CHECKS_PASSED', res_str['readiness'])
            self.assertEqual(0, res_str['exitCode'])

            # 2) Later migration `DISABLE ROW LEVEL SECURITY` must flag BLOCKED
            rls_dir = root / 'rls_dis'
            rls_dir.mkdir()
            (rls_dir / '001.sql').write_text(
                'CREATE TABLE public.orders(id uuid, user_id uuid);\n'
                'ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;\n'
                'CREATE POLICY own ON public.orders USING (auth.uid() = user_id);\n',
                encoding='utf-8',
            )
            (rls_dir / '002.sql').write_text('ALTER TABLE public.orders DISABLE ROW LEVEL SECURITY;\n', encoding='utf-8')
            res_rls = build_rescue_bundle(rls_dir)
            self.assertEqual('BLOCKED', res_rls['readiness'])
            self.assertEqual(1, res_rls['exitCode'])

            # 3) `writtenFiles` paths must be scrubbed when `write_files=True`
            sec_dir = root / ('vr_' + 'sk_live_' + 'syntheticAuditOnly12345')
            sec_dir.mkdir()
            (sec_dir / 'index.js').write_text('export const ok = 1;\n', encoding='utf-8')
            res_sec = build_rescue_bundle(sec_dir, write_files=True)
            self.assertTrue(res_sec['secretSafety']['verifiedOutputClean'])
            self.assertNotIn('sk_live_' + 'syntheticAuditOnly12345', json.dumps(res_sec['writtenFiles']))

    def test_v84_anti_slop_rgb_contrast_and_backup_preservation(self):
        from prism_memory.anti_slop import scan_anti_slop, lock_design_system

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) `rgb(...)` low contrast detection
            rgb_dir = root / 'rgb_case'
            rgb_dir.mkdir()
            lock_design_system(rgb_dir, write_files=True)
            (rgb_dir / 'style.css').write_text('.btn { background: rgb(255, 255, 255); color: rgb(250, 250, 250); }\n', encoding='utf-8')
            res_rgb = scan_anti_slop(rgb_dir)
            self.assertFalse(res_rgb['ok'])
            self.assertGreaterEqual(res_rgb['byCategory'].get('low_contrast_text', 0), 1)

            # 2) Repeated `lock_design_system(..., force=True)` preserves original `.bak`
            bak_dir = root / 'bak_case'
            bak_dir.mkdir()
            (bak_dir / 'DESIGN.md').write_text('ORIGINAL', encoding='utf-8')
            (bak_dir / 'design-tokens.css').write_text('ORIGINAL_CSS', encoding='utf-8')
            lock_design_system(bak_dir, write_files=True, force=True)
            lock_design_system(bak_dir, write_files=True, force=True)
            self.assertEqual('ORIGINAL', (bak_dir / 'DESIGN.md.bak').read_text(encoding='utf-8'))
            self.assertEqual('ORIGINAL_CSS', (bak_dir / 'design-tokens.css.bak').read_text(encoding='utf-8'))

    def test_v84_cli_and_action_exit_code_parity(self):
        from prism_memory.viberescue import cli as vr_cli
        import memory_cli, io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as td:
            warn_dir = Path(td) / 'warn_case'
            warn_dir.mkdir()
            (warn_dir / '001.sql').write_text(
                'CREATE TABLE public.items(id uuid);\n'
                'ALTER TABLE public.items ENABLE ROW LEVEL SECURITY;\n'
                'CREATE POLICY pub_read ON public.items USING (true);\n',
                encoding='utf-8',
            )
            with redirect_stdout(io.StringIO()):
                code_vr = vr_cli(['pack', str(warn_dir), '--format', 'json'])
            old_argv = sys.argv
            try:
                sys.argv = ['prism-memory', 'rescue', str(warn_dir)]
                with redirect_stdout(io.StringIO()):
                    code_mem = memory_cli.cli()
            finally:
                sys.argv = old_argv
            self.assertEqual(1, code_vr)
            self.assertEqual(1, code_mem)

    def test_v70_ast_signature_self_healing_vs_breaking_change_guard(self):
        target_py = self.root / 'engine_core.py'
        target_py.write_text(
            'def compute_score(items, weight=1.0):\n    return len(items) * weight\n',
            encoding='utf-8',
        )
        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-v70-ast', 'report', 'rep-v70-ast', 'assert',
                f'{target_py} 파일의 compute_score 함수를 호출해 점수를 계산했다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=300,
            )
        res = self.learn_experience(
            item_id='experience:v70-ast-heal',
            card={
                'situation': f'{target_py} 파일의 compute_score 함수로 점수를 계산할 때',
                'steps': [
                    f'{target_py} 파일의 compute_score 입력 리스트를 준비한다.',
                    'compute_score 결과를 검증하고 반환한다.',
                ],
                'outcome': '점수 계산 성공',
                'conditions': f'{target_py} 모듈이 존재할 때만 적용',
                'counterexamples': '입력 리스트가 비어 있으면 0을 반환한다.',
            },
            quote=f'{target_py} 파일의 compute_score 함수를 호출해 점수를 계산했다.',
            ref_id='rep-v70-ast',
        )
        sk_id = res['skills'][0]['skillId']

        # 1) Backward-compatible edit: internal implementation change + optional keyword parameter with default
        target_py.write_text(
            'def compute_score(items, weight=1.0, bonus=0.0):\n    # optimized internal body\n    total = float(len(items)) * weight + bonus\n    return round(total, 4)\n',
            encoding='utf-8',
        )
        before_heal = self.store.skills('task-one')['skills']
        sk_before = next(s for s in before_heal if s['skill_id'] == sk_id)
        self.assertTrue(sk_before['stale'])
        self.assertTrue(sk_before['staleness']['selfHealable'])

        # Missing session or non-owner session must be rejected on write heal
        with self.assertRaises(ValueError):
            self.service.action({'action': 'memory_heal_skills', 'taskId': 'task-one'})

        heal_res = self.service.action({'action': 'memory_heal_skills', 'taskId': 'task-one', 'session': OWNER})
        self.assertTrue(heal_res['ok'])
        self.assertIn(sk_id, [h['skillId'] for h in heal_res['healedSkills']])

        after_heal = self.store.skills('task-one')['skills']
        sk_after = next(s for s in after_heal if s['skill_id'] == sk_id)
        self.assertFalse(sk_after['stale'])

        # 2) Breaking change: add a required positional parameter `strict_mode` without default
        target_py.write_text(
            'def compute_score(items, strict_mode, weight=1.0):\n    return len(items) * weight\n',
            encoding='utf-8',
        )
        heal_break = self.service.action({'action': 'memory_heal_skills', 'taskId': 'task-one', 'session': OWNER})
        self.assertNotIn(sk_id, [h['skillId'] for h in heal_break['healedSkills']])
        self.assertTrue(any(b['skillId'] == sk_id for b in heal_break['blockedBreakingSkills']))
        sk_broken = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertTrue(sk_broken['stale'])
        self.assertFalse(sk_broken['staleness']['selfHealable'])

    def test_v70_zero_touch_turn_attribution_and_auto_promotion(self):
        res = self.learn_experience()
        sk_id = res['skills'][0]['skillId']

        # Turn 1: initial turn context records sk_id in last_primary_ids
        view1 = self.store.memory('task-one', 'server.py 웹 프로세스만 교체하고 실행 원장을 보존하는 절차', 4000)
        _, meta1 = memory.build_turn_context(self.store, 'task-one', OWNER, view1, 'rec-t1', query='server.py 웹 프로세스만 교체하고 실행 원장을 보존하는 절차')
        self.assertFalse(meta1['deltaMode'])

        # Turn 2: user responds positively ("응 잘 작동해 다음 단계 진행해줘") -> auto-promotes reported -> trial-used
        view2 = self.store.memory('task-one', '응 잘 작동해 다음 단계 진행해줘', 4000)
        _, meta2 = memory.build_turn_context(self.store, 'task-one', OWNER, view2, 'rec-t2', query='응 잘 작동해 다음 단계 진행해줘')
        self.assertTrue(meta2['trajectoryAttribution']['attributed'])
        sk_t2 = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('trial-used', sk_t2['evidence_state'])
        self.assertGreaterEqual(sk_t2['used_count'], 1)

        # Ensure sk_id is in last_primary_ids for Turn 3 rehearsal
        with self.store.db(True) as db:
            db.execute(
                'UPDATE memory_session_state SET last_primary_ids=?, last_turn_query=? WHERE task=? AND session=?',
                (json.dumps([sk_id]), 'server.py 웹 프로세스 교체 검증', 'task-one', OWNER),
            )

        # Turn 3: user responds with action directive ("제안대로 모두 진행해줘") -> counts trial use (`used_count >= 2`)
        # but v8.1 governance caps implicit turn attribution at `trial-used` (never falsely promoting without explicit feedback or execution proof)
        view3 = self.store.memory('task-one', '제안대로 모두 진행해줘', 4000)
        _, meta3 = memory.build_turn_context(self.store, 'task-one', OWNER, view3, 'rec-t3', query='제안대로 모두 진행해줘')
        self.assertTrue(meta3['trajectoryAttribution']['attributed'])
        self.assertTrue(meta3['trajectoryAttribution'].get('hasDirective'))
        sk_t3 = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('trial-used', sk_t3['evidence_state'])
        self.assertGreaterEqual(sk_t3['used_count'], 2)

        # Execution-backed verification promotes `trial-used -> feedback-verified` with cryptographic verificationEvidence
        ver_res = memory.verify_skill(
            self.store, 'task-one', OWNER, sk_id,
            execution_proof={'command': 'pytest -q', 'exitCode': 0, 'output': '1 passed'},
        )
        self.assertTrue(ver_res['ok'])
        self.assertEqual('feedback-verified', ver_res['evidenceState'])
        self.assertEqual('verified', ver_res['verificationStatus'])

        st = memory.stats(self.store, 'task-one')
        self.assertGreaterEqual(st['feedback'].get('used', 0), 2)
        self.assertGreater(st['overallAdoptionRate'], 0.0)

    def test_v70_cold_start_zero_onnx_fast_path(self):
        (self.root / 'memory_settings.json').write_text(json.dumps({'semanticEnabled': True}), encoding='utf-8')
        self.learn_experience()
        saved_model = memory.vectors._MODEL
        try:
            memory.vectors._MODEL = None
            t0 = time.monotonic()
            mem_view = self.store.memory('task-one', 'server.py 웹 프로세스만 교체하고 실행 원장을 보존', 4000)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self.assertTrue(mem_view['retrieval']['onnxColdBypassed'])
            self.assertIsNone(memory.vectors._MODEL)
            self.assertLess(elapsed_ms, 50.0)
            self.assertGreaterEqual(len(mem_view['retrieval']['items']), 1)
        finally:
            memory.vectors._MODEL = saved_model

    def test_v70_contrastive_failure_to_case_and_subagent_capsule_and_mcp_v7(self):
        from prism_memory_mcp import PrismMemoryMCPServer
        res = self.learn_experience()
        sk_id = res['skills'][0]['skillId']

        # 1) Contrastive Failure-to-Case synthesis via auto_capture
        cap = memory.auto_capture(
            self.store, 'task-one', OWNER,
            {
                'text': '실패: server.py 전체 재시작 시도로 진행 중 입력 lease 유실 발생 -> 해결: 입력 전달 중인지 확인 후 웹 프로세스만 교체 적용',
                'entities': ['server.py', '웹', '프로세스', '원장'],
                'operationId': 'op-contrastive-v70',
            },
        )
        self.assertTrue(cap['ok'])
        self.assertTrue(cap['admitted'])
        self.assertEqual('case', cap['detectedKind'])
        self.assertIsNotNone(cap['contrastiveCase'])
        self.assertIn('contrastiveDiff', cap['contrastiveCase'])
        self.assertIn(sk_id, cap['enrichedSkills'])
        updated_sk = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertIn('[대조 실패 경계]', updated_sk['counterexamples'])

        # 2) Subagent Scoped Memory Capsule (<1,200 chars budget)
        capsule_res = self.service.action({
            'action': 'memory_capsule',
            'taskId': 'task-one',
            'plan': 'server.py 웹 프로세스만 교체 및 원장 보존 점검',
            'files': ['server.py'],
            'worker': 'claude-sonnet-worker',
            'budget': 1100,
        })
        self.assertTrue(capsule_res['ok'])
        self.assertTrue(capsule_res['withinBudget'])
        self.assertLessEqual(capsule_res['capsuleChars'], 1100)
        self.assertIn('<subagent-memory-capsule', capsule_res['capsuleText'])

        # 3) MCP Server v7.0.0 / v8.0.0 with >=15 tools including prism_memory_capsule
        mcp = PrismMemoryMCPServer(self.root, default_task='task-one', default_session=OWNER, standalone=False)
        init_resp = mcp.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertIn(init_resp['result']['serverInfo']['version'], ('7.0.0', '8.0.0'))
        tools_resp = mcp.handle_rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        tool_names = [t['name'] for t in tools_resp['result']['tools']]
        self.assertGreaterEqual(len(tool_names), 15)
        self.assertIn('prism_memory_capsule', tool_names)

    def test_v80_negation_aware_trajectory_attribution_and_skill_event_ledger(self):
        from prism_memory.heuristics import analyze_trajectory_sentiment
        res = self.learn_experience()
        sk_id = res['skills'][0]['skillId']

        # 1) Privative positive ("오류 없이 통과했어", "no error") must NOT be misclassified as negative
        s_priv_ko = analyze_trajectory_sentiment('오류 없이 통과했어 다음 단계 진행해줘')
        self.assertEqual('positive', s_priv_ko['sentiment'])
        self.assertIn('privative_positive', s_priv_ko['reason'])

        s_priv_en = analyze_trajectory_sentiment('Works without error, proceed.')
        self.assertEqual('positive', s_priv_en['sentiment'])

        # 2) Deferred execution ("응 아직 실행하지 않았어") must NOT be misclassified as positive
        s_def = analyze_trajectory_sentiment('응 아직 실행하지 않았어 조금 있다가 해볼게')
        self.assertIn(s_def['sentiment'], ('neutral', 'deferred'))
        self.assertIn(s_def['reason'], ('deferred_execution', 'deferred_unexecuted'))

        # 3) Negated positive ("Not good", "잘 안 돼") must be classified as negative
        s_neg_en = analyze_trajectory_sentiment('Not good, still failing on socket bind')
        self.assertEqual('negative', s_neg_en['sentiment'])

        s_neg_ko = analyze_trajectory_sentiment('잘 작동하지 않아 다시 확인해줘')
        self.assertEqual('negative', s_neg_ko['sentiment'])

        # 4) Verify positive trajectory promotion writes to memory_skill_events AND updates SKILL.md on disk
        view1 = self.store.memory('task-one', 'server.py 웹 프로세스만 교체하고 실행 원장을 보존하는 절차', 4000)
        memory.build_turn_context(self.store, 'task-one', OWNER, view1, 'rec-v80-1', query='server.py 웹 프로세스만 교체하고 실행 원장을 보존하는 절차')

        # Deferred reply does NOT promote skill
        view_def = self.store.memory('task-one', '응 아직 실행하지 않았어', 4000)
        _, meta_def = memory.build_turn_context(self.store, 'task-one', OWNER, view_def, 'rec-v80-def', query='응 아직 실행하지 않았어')
        self.assertFalse(meta_def['trajectoryAttribution']['attributed'])
        self.assertEqual('reported', memory.read_skill(self.store, 'task-one', sk_id)['evidence_state'])

        # Re-seed last_primary_ids and test privative positive ("오류 없이 잘 작동해 진행해줘")
        with self.store.db(True) as db:
            db.execute(
                'UPDATE memory_session_state SET last_primary_ids=?, last_turn_query=? WHERE task=? AND session=?',
                (json.dumps([sk_id]), 'server.py 웹 프로세스 교체', 'task-one', OWNER),
            )
        view2 = self.store.memory('task-one', '오류 없이 잘 작동해 다음 단계 진행해줘', 4000)
        _, meta2 = memory.build_turn_context(self.store, 'task-one', OWNER, view2, 'rec-v80-2', query='오류 없이 잘 작동해 다음 단계 진행해줘')
        self.assertTrue(meta2['trajectoryAttribution']['attributed'])
        sk_after = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('trial-used', sk_after['evidence_state'])
        self.assertTrue(any(ev['operation'] == 'trajectory-promote' for ev in sk_after['events']))
        md_on_disk = Path(sk_after['skill_path']).read_text(encoding='utf-8')
        self.assertIn('근거 수준: `trial-used`', md_on_disk)

    def test_v80_strict_ast_call_contract_guard_and_compatible_unverified_status(self):
        target_py = self.root / 'contract_mod.py'
        target_py.write_text(
            'def process_batch(items, timeout=5.0, *, strict=False):\n    return len(items)\n',
            encoding='utf-8',
        )
        unanchored_py = self.root / 'unanchored_helper.py'
        unanchored_py.write_text('CONST_VAL = 1\n', encoding='utf-8')

        with self.store.db(True) as db:
            ledger.record(
                db, 'task-one', 1, 'rep-v80-ast', 'report', 'rep-v80-ast', 'assert',
                f'{target_py} 파일의 process_batch 함수와 {unanchored_py} 설정을 점검했다.',
                {'kind': 'observed-response', 'session': OWNER}, event_at=310,
            )
        res = self.learn_experience(
            item_id='experience:v80-ast-contract',
            card={
                'situation': f'{target_py} 파일의 process_batch 함수를 호출할 때',
                'steps': [
                    f'{target_py} 파일의 process_batch 입력을 검증한다.',
                    '배치 처리 결과를 반환한다.',
                ],
                'outcome': '배치 처리 완료',
                'conditions': f'{target_py} 모듈 사용 시에만 적용',
                'counterexamples': '빈 배치는 호출하지 않는다.',
            },
            quote=f'{target_py} 파일의 process_batch 함수와',
            ref_id='rep-v80-ast',
        )
        sk_id = res['skills'][0]['skillId']

        # Case A: Removing an optional parameter (timeout=5.0 removed) -> MUST be blocked as breaking!
        target_py.write_text(
            'def process_batch(items, *, strict=False):\n    return len(items)\n',
            encoding='utf-8',
        )
        sk_opt_removed = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertTrue(sk_opt_removed['stale'])
        self.assertFalse(sk_opt_removed['staleness']['selfHealable'])
        self.assertTrue(any(r.startswith('breaking_change:') for r in sk_opt_removed['staleness']['reasons']))

        # Case B: Converting regular parameter `items` to positional-only `items, /` -> MUST be blocked as breaking!
        target_py.write_text(
            'def process_batch(items, /, timeout=5.0, *, strict=False):\n    return len(items)\n',
            encoding='utf-8',
        )
        sk_posonly = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertTrue(sk_posonly['stale'])
        self.assertFalse(sk_posonly['staleness']['selfHealable'])

        # Case C: Removing keyword-only parameter `strict=False` -> MUST be blocked as breaking!
        target_py.write_text(
            'def process_batch(items, timeout=5.0):\n    return len(items)\n',
            encoding='utf-8',
        )
        sk_kw_removed = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertTrue(sk_kw_removed['stale'])
        self.assertFalse(sk_kw_removed['staleness']['selfHealable'])

        # Case D: Compatible internal body edit + adding new optional parameter ` retries=2`
        target_py.write_text(
            'def process_batch(items, timeout=5.0, retries=2, *, strict=False):\n    total = len(items) * (1 if retries else 1)\n    return total\n',
            encoding='utf-8',
        )
        sk_compat = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertTrue(sk_compat['stale'])
        self.assertTrue(sk_compat['staleness']['selfHealable'])

        # Non-owner session is rejected when attempting write heal_skills
        with self.assertRaises(ValueError):
            memory.heal_skills(self.store, 'task-one', session='unauthorized-actor', dry_run=False)

        # Dry-run previews without modifying DB
        dry_res = memory.heal_skills(self.store, 'task-one', dry_run=True)
        self.assertTrue(dry_res['dryRun'])
        self.assertIn(sk_id, [h['skillId'] for h in dry_res['healedSkills']])
        self.assertTrue(next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)['stale'])

        # Authorized heal updates anchor and marks verificationStatus = 'compatible-unverified'
        heal_res = memory.heal_skills(self.store, 'task-one', session=OWNER, dry_run=False)
        self.assertEqual('compatible-unverified', heal_res['healedSkills'][0]['verificationStatus'])
        sk_healed = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == sk_id)
        self.assertFalse(sk_healed['stale'])
        self.assertEqual('compatible-unverified', sk_healed['staleness']['verificationStatus'])

        # Case E: Unanchored code file modification must NOT be auto-healed
        res_unanchored = self.learn_experience(
            item_id='experience:v80-unanchored',
            card={
                'situation': f'{unanchored_py} 설정 스크립트를 실행할 때',
                'steps': [
                    f'{unanchored_py} 파일을 읽는다.',
                    '결과를 출력한다.',
                ],
                'outcome': '스크립트 확인 완료',
                'conditions': f'{unanchored_py} 파일이 있을 때만 적용',
                'counterexamples': '파일이 없으면 생략한다.',
            },
            quote=f'{unanchored_py} 설정을 점검했다.',
            ref_id='rep-v80-ast',
            operation='op-v80-unanchored',
        )
        un_sk_id = res_unanchored['skills'][0]['skillId']
        unanchored_py.write_text('CONST_VAL = 999\n', encoding='utf-8')
        sk_un = next(s for s in self.store.skills('task-one')['skills'] if s['skill_id'] == un_sk_id)
        self.assertTrue(sk_un['stale'])
        self.assertFalse(sk_un['staleness']['selfHealable'])
        self.assertTrue(any('unanchored_code_file_changed' in r for r in sk_un['staleness']['reasons']))

    def test_v80_markdown_coedit_session_guard_and_contrastive_skill_event_sync(self):
        res = self.learn_experience()
        sk_id = res['skills'][0]['skillId']
        sk_info = memory.read_skill(self.store, 'task-one', sk_id)
        md_path = Path(sk_info['skill_path'])

        # Modify SKILL.md on disk (simulating an external edit)
        raw_md = md_path.read_text(encoding='utf-8')
        modified_md = raw_md.replace(
            '기존 담당자·실행·전송 기록 손실 없이 새 기억·스킬 코드가 운영에 반영됨',
            '기존 담당자·실행·전송 기록 손실 없이 무중단 포트 검증 및 운영 반영 완료됨',
        )
        md_path.write_text(modified_md, encoding='utf-8')

        # 1) Passive read via store.skills('task-one') without explicit user session does NOT promote to user-confirmed
        passive_skills = self.store.skills('task-one')['skills']
        sk_passive = next(s for s in passive_skills if s['skill_id'] == sk_id)
        self.assertNotEqual('user-confirmed', sk_passive['evidence_state'])

        # 2) Default sync_markdown with owner session ingests edit as unverified-disk-sync, while explicit_user=True promotes to user-confirmed
        sync_unverified = memory.sync_markdown(self.store, 'task-one', session=OWNER)
        self.assertTrue(sync_unverified['ok'])
        self.assertEqual(1, len(sync_unverified['skillCoEdits']))
        sk_unver = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('reported', sk_unver['evidence_state'])
        self.assertEqual('compatible-unverified', sk_unver['source'].get('verificationStatus'))

        md_path.write_text(
            md_path.read_text(encoding='utf-8').replace(
                '무중단 포트 검증 및 운영 반영 완료됨',
                '무중단 포트 검증·해시 대조 및 운영 반영 완료됨',
            ),
            encoding='utf-8',
        )
        sync_res = memory.sync_markdown(self.store, 'task-one', session=OWNER, explicit_user=True)
        self.assertTrue(sync_res['ok'])
        self.assertEqual(1, len(sync_res['skillCoEdits']))
        sk_synced = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('user-confirmed', sk_synced['evidence_state'])

        # 3) Contrastive failure-to-case enrichment records an event in memory_skill_events
        events_before = len(sk_synced['events'])
        cap = memory.auto_capture(
            self.store, 'task-one', OWNER,
            {
                'text': '실패: server.py 포트 충돌 상태에서 강제 기동 -> 해결: lsof 확인 후 웹 프로세스만 교체',
                'entities': ['server.py', '웹', '프로세스'],
                'operationId': 'op-v80-contrastive-ev',
            },
        )
        self.assertIn(sk_id, cap['enrichedSkills'])
        sk_enriched = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertGreater(len(sk_enriched['events']), events_before)
        self.assertTrue(any(ev['operation'] == 'contrastive-enrich' for ev in sk_enriched['events']))

    def test_v80_bounded_skill_metrics_and_live_provenance_benchmark(self):
        res = self.learn_experience()
        sk_id = res['skills'][0]['skillId']

        # Record 2 recalls exposing sk_id, and set used_count=5, helpful_count=4 (> recall count!)
        ret = self.store.memory('task-one', 'server.py 웹 프로세스만 교체')['retrieval']
        memory.record_recall(self.store, 'task-one', OWNER, 'rec-bound-1', 'server.py 웹 프로세스', ret)
        memory.record_recall(self.store, 'task-one', OWNER, 'rec-bound-2', 'server.py 웹 프로세스', ret)
        with self.store.db(True) as db:
            db.execute(
                'UPDATE memory_skills SET used_count=5, helpful_count=4 WHERE task=? AND skill_id=?',
                ('task-one', sk_id),
            )
        st = memory.stats(self.store, 'task-one')
        self.assertLessEqual(st['overallAdoptionRate'], 1.0)
        self.assertLessEqual(st['overallHelpfulRate'], 1.0)
        self.assertEqual(1.0, st['overallAdoptionRate'])
        self.assertGreater(st['reuseIntensity'], 1.0)

        bench = memory.benchmark(self.store, 'task-one', budget=3000)
        self.assertEqual('8.0', bench['version'])
        self.assertIn('liveProvenance', bench['governance'])
        self.assertGreaterEqual(bench['governance']['liveProvenance']['checkedCount'], 1)
        self.assertEqual(0.0, bench['governance']['identityDriftScore'])
        self.assertIn('temporalGuardDetail', bench['retrievalQuality'])

    def test_v80_standalone_memory_engine_sdk_and_mcp_v8(self):
        from prism_memory import MemoryEngine, StandaloneStore
        from prism_memory_mcp import PrismMemoryMCPServer

        with tempfile.TemporaryDirectory() as tmp:
            engine = MemoryEngine(tmp, default_task='sdk/demo', default_session='sdk-user')
            self.assertIsInstance(engine.store, StandaloneStore)

            # 1) Capture an observation and synthesize a skill without WorkStore
            cap = engine.capture(
                '1. sqlite3 WAL 모드를 활성화한다.\n2. 트랜잭션 커밋 후 체크포인트를 확인한다.',
                command='db_init.py',
            )
            self.assertTrue(cap['ok'])
            self.assertEqual('experience', cap['detectedKind'])
            self.assertEqual(1, len(cap['skills']))

            # 2) Recall via O(postings) index
            rec = engine.recall('sqlite3 WAL 체크포인트', budget=2400)
            self.assertGreaterEqual(len(rec['retrieval']['items']), 1)
            self.assertIn('recallId', rec)

            # 3) Preflight, Capsule, Heal-Skills, Inspector, Benchmark
            pf = engine.preflight('db_init.py WAL 점검', files=['db_init.py'])
            self.assertTrue(pf['ok'])
            caps = engine.capsule('db_init.py 서브에이전트 점검', files=['db_init.py'], budget=1000)
            self.assertTrue(caps['ok'])
            self.assertLessEqual(caps['capsuleChars'], 1000)
            heal = engine.heal_skills(dry_run=True)
            self.assertTrue(heal['ok'])
            insp = engine.inspector(budget=2400)
            self.assertTrue(insp['ok'])
            bench = engine.benchmark(budget=2400)
            self.assertEqual('8.0', bench['version'])

            # 4) Verify MCP Server v8.0.0 works directly with StandaloneStore and exposes 18 tools
            mcp = PrismMemoryMCPServer(tmp, default_task='sdk/demo', default_session='sdk-user', store=engine.store)
            init_res = mcp.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
            self.assertEqual('8.0.0', init_res['result']['serverInfo']['version'])
            tools_res = mcp.handle_rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
            tool_names = [t['name'] for t in tools_res['result']['tools']]
            self.assertGreaterEqual(len(tool_names), 18)
            self.assertIn('prism_memory_heal_skills', tool_names)
            self.assertIn('prism_memory_verify_skill', tool_names)
            self.assertIn('prism_memory_doctor', tool_names)
            self.assertIn('prism_memory_audit', tool_names)

    def test_v80_astra_round2_p1_fixes(self):
        from prism_memory import MemoryEngine, extract_file_symbol_signatures, extract_file_symbol_hashes, is_breaking_symbol_change
        import memory_cli, io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            # 1) P1-2: Class-qualified method signatures & ambiguous method collision guard
            py_file = Path(tmp1) / 'dual_classes.py'
            py_file.write_text(
                'class Alpha:\n    def execute(self, x):\n        return x\n\n'
                'class Beta:\n    def execute(self, x):\n        return x + 1\n',
                encoding='utf-8',
            )
            sigs_v1 = extract_file_symbol_signatures(py_file)['symbols']
            hashes_v1 = extract_file_symbol_hashes(py_file)
            self.assertIn('Alpha.execute', sigs_v1)
            self.assertIn('Beta.execute', sigs_v1)
            self.assertEqual('ambiguous_method', sigs_v1['execute']['kind'])

            # Break Alpha.execute while keeping Beta.execute unchanged
            py_file.write_text(
                'class Alpha:\n    def execute(self, x, required_flag):\n        return x\n\n'
                'class Beta:\n    def execute(self, x):\n        return x + 1\n',
                encoding='utf-8',
            )
            sigs_v2 = extract_file_symbol_signatures(py_file)['symbols']
            hashes_v2 = extract_file_symbol_hashes(py_file)
            self.assertNotEqual(hashes_v1['execute'], hashes_v2['execute'])
            brk_qual, _ = is_breaking_symbol_change(sigs_v1['Alpha.execute'], sigs_v2['Alpha.execute'])
            brk_ambig, rsn_ambig = is_breaking_symbol_change(sigs_v1['execute'], sigs_v2['execute'])
            self.assertTrue(brk_qual)
            self.assertTrue(brk_ambig)
            self.assertIn('Alpha.execute', rsn_ambig)

            # 2) P1-1: Separate DBs with identical task IDs and row counts do not share lexical index cache
            eng_a = MemoryEngine(tmp1, default_task='shared/task', default_session='s1')
            eng_b = MemoryEngine(tmp2, default_task='shared/task', default_session='s1')
            eng_a.capture('1. alpha_unique_token_991 절차를 점검한다.\n2. 결과를 기록한다.')
            eng_b.capture('1. beta_unique_token_882 절차를 점검한다.\n2. 결과를 기록한다.')
            rec_a1 = eng_a.recall('alpha_unique_token_991')
            rec_b1 = eng_b.recall('beta_unique_token_882')
            rec_a2 = eng_a.recall('alpha_unique_token_991')
            self.assertTrue(any('alpha_unique_token_991' in x['excerpt'] for x in rec_a1['retrieval']['items']))
            self.assertTrue(any('beta_unique_token_882' in x['excerpt'] for x in rec_b1['retrieval']['items']))
            self.assertTrue(any('alpha_unique_token_991' in x['excerpt'] for x in rec_a2['retrieval']['items']))

            # 3) P1-3: >120 candidate documents with exact BM25 scoring preserves highest-TF document at index >120
            with eng_a.store.db(True) as db:
                for idx in range(135):
                    ledger.record(
                        db, 'shared/task', idx + 10, f'ev:bulk-{idx}', 'fact', f'fact:bulk-{idx}', 'assert',
                        f'common_shared_keyword 배경 설명 문서 번호 {idx} 추가 단어 나열 텍스트입니다.',
                        source={'kind': 'user-note', 'certainty': 'explicit-user'},
                        event_at=1000.0 + idx,
                    )
                ledger.record(
                    db, 'shared/task', 200, 'ev:high-tf-winner', 'fact', 'fact:high-tf-winner', 'assert',
                    'common_shared_keyword common_shared_keyword common_shared_keyword 핵심 요약',
                    source={'kind': 'user-note', 'certainty': 'explicit-user'},
                    event_at=900.0,
                )
            rec_many = eng_a.recall('common_shared_keyword', budget=4000)
            top_ids = [x['item_id'] for x in rec_many['retrieval']['items'][:3]]
            self.assertIn('fact:high-tf-winner', top_ids)

            # 4) P1-4: memory_cli.cli() prints JSON and returns exit code 0
            buf = io.StringIO()
            old_argv = sys.argv
            try:
                sys.argv = ['prism-memory', '--root', tmp1, '--task', 'shared/task', 'schema']
                with redirect_stdout(buf):
                    code = memory_cli.cli()
            finally:
                sys.argv = old_argv
            self.assertEqual(0, code)
            parsed_cli = json.loads(buf.getvalue())
            self.assertIn('learn', parsed_cli)

    def test_v80_astra_round3_counterexample_fixes(self):
        import hashlib, sqlite3
        from prism_memory import extract_file_symbol_signatures, evaluation

        with tempfile.TemporaryDirectory() as td:
            # 1) Conditional function inside `if True:` is collected by AST anchor scanner
            p = Path(td) / 'cond.py'
            p.write_text('if True:\n    def cond_fn(x):\n        return x\n', encoding='utf-8')
            sigs = extract_file_symbol_signatures(p)['symbols']
            self.assertIn('cond_fn', sigs)

            # 2) Separate DBs with identical item_id/event_id/content but different source.entities ("alpha" vs "beta")
            db_a = sqlite3.connect(':memory:')
            db_b = sqlite3.connect(':memory:')
            db_a.row_factory = sqlite3.Row
            db_b.row_factory = sqlite3.Row
            for db_conn, ent_label in ((db_a, 'alpha'), (db_b, 'beta')):
                ledger.ensure_schema(db_conn)
                memory.ensure_schema(db_conn)
                ledger.record(
                    db_conn, 'same-task', 1, 'ev-1', 'fact', 'fact:same', 'assert',
                    '동일한 본문 텍스트입니다.',
                    source={'kind': 'explicit-memory-edit', 'entities': [ent_label]},
                    event_at=1000.0,
                )
            res_a = memory.retrieve(db_a, 'same-task', query='alpha', limit=5)
            res_b = memory.retrieve(db_b, 'same-task', query='beta', limit=5)
            self.assertEqual(1, len(res_a['items']))
            self.assertEqual(1, len(res_b['items']))

            # 3) Exact phrase match on 121st document (`alpha beta` after 120 `beta alpha` docs)
            db_c = sqlite3.connect(':memory:')
            db_c.row_factory = sqlite3.Row
            ledger.ensure_schema(db_c)
            memory.ensure_schema(db_c)
            for i in range(120):
                ledger.record(
                    db_c, 'phrase-task', i + 1, f'ev-{i}', 'fact', f'fact:doc-{i:03d}', 'assert',
                    f'beta alpha padding {i}',
                    source={'kind': 'explicit-memory-edit'},
                    event_at=1000.0 + i,
                )
            ledger.record(
                db_c, 'phrase-task', 121, 'ev-120', 'fact', 'fact:doc-120-exact', 'assert',
                'alpha beta padding 120',
                source={'kind': 'explicit-memory-edit'},
                event_at=900.0,
            )
            res_c = memory.retrieve(db_c, 'phrase-task', query='alpha beta', limit=5)
            self.assertEqual('fact:doc-120-exact', res_c['items'][0]['item_id'])

            # 4) Provenance audit rejects mismatched eventId vs itemId & flags derived overwrite of protected fact
            db_d = sqlite3.connect(':memory:')
            db_d.row_factory = sqlite3.Row
            ledger.ensure_schema(db_d)
            ledger.record(db_d, 'prov-task', 1, 'e1', 'instruction', 'input:a', 'assert', 'Source A text', source={'kind': 'user-input'}, event_at=1000.0)
            ledger.record(db_d, 'prov-task', 2, 'e2', 'instruction', 'input:b', 'assert', 'Source B text', source={'kind': 'user-input'}, event_at=1001.0)
            ev2_id = ledger.get_item(db_d, 'prov-task', 'input:b')['event_id']
            sha_b = hashlib.sha256(b'Source B text').hexdigest()
            ledger.record(
                db_d, 'prov-task', 3, 'e3', 'fact', 'fact:bad-ref', 'assert',
                'Claim with mismatched ref',
                source={'kind': 'agent-learn', 'certainty': 'explicit-user', 'sourceRefs': [{'itemId': 'input:a', 'eventId': ev2_id, 'contentSha256': sha_b, 'quote': 'Source B'}]},
                event_at=1002.0,
            )
            ledger.record(
                db_d, 'prov-task', 4, 'e4', 'fact', 'fact:bad-ref', 'replace',
                'Derived overwrite of protected fact',
                source={'kind': 'agent-learn', 'certainty': 'derived', 'sourceRefs': [{'itemId': 'input:a', 'eventId': ev2_id, 'contentSha256': sha_b, 'quote': 'Source B'}]},
                event_at=1003.0,
            )
            active_d = ledger.current(db_d, 'prov-task')
            audit_d = evaluation.verify_live_provenance_and_drift(db_d, 'prov-task', active_d, {'goal', 'constraint', 'decision', 'preference'})
            self.assertEqual(0, audit_d['cryptographicallyVerifiedCount'])
            self.assertEqual(1, audit_d['identityDriftViolations'])

    def test_v81_execution_verification_cleanroom_wheel_and_repo_benchmark(self):
        import os, shutil
        from prism_memory import (
            MemoryEngine,
            resolve_default_data_root,
            doctor_environment,
            backup_data_root,
            restore_data_root,
            run_repo_evolution_benchmark,
        )
        from prism_memory_mcp import PrismMemoryMCPServer

        # 1. Execution-backed skill verification (pass -> feedback-verified + verificationEvidence, fail -> needs_revision + pitfall case)
        target_py = self.root / 'calc_mod.py'
        target_py.write_text('def calc_tax(amount, rate=0.1):\n    return round(amount * rate, 2)\n', encoding='utf-8')
        res = self.learn_experience(
            item_id='experience:calc-tax-flow',
            card=self.sample_card(
                situation=f'{target_py} 파일의 calc_tax 함수를 호출해 세금을 계산할 때',
                steps=[f'{target_py} 파일의 calc_tax(amount, rate)를 호출한다.', '반환된 세액을 정산 원장에 기록한다.'],
                outcome='정확한 세액이 계산된다.',
                conditions=f'{target_py} 파일의 calc_tax 시그니처가 유효할 때 적용한다.',
                counterexamples='음수 금액이거나 calc_tax 시그니처가 깨진 경우 호출하지 않는다.',
            ),
            skill={'id': 'skill:calc-tax-flow', 'name': 'calc-tax-flow', 'title': '세액 계산 절차'},
        )
        sk_id = res['skills'][0]['skillId']

        # Verify via real subprocess command (exitCode == 0)
        ver_ok = self.service.action({
            'action': 'memory_verify_skill',
            'taskId': 'task-one',
            'session': OWNER,
            'skillId': sk_id,
            'command': 'python3 -c "import calc_mod; assert calc_mod.calc_tax(100, 0.1) == 10.0"',
        })
        self.assertTrue(ver_ok['ok'])
        self.assertEqual('feedback-verified', ver_ok['evidenceState'])
        self.assertEqual('verified', ver_ok['verificationStatus'])
        self.assertEqual(0, ver_ok['verificationEvidence']['exitCode'])
        self.assertTrue(ver_ok['verificationEvidence']['codeAnchorDigest'])
        self.assertTrue(ver_ok['verificationEvidence']['outputSha256'])

        # Modify calc_mod.py compatibly -> heal_skills downgrades feedback-verified to trial-used & compatible-unverified
        target_py.write_text(
            'def calc_tax(amount, rate=0.1, rounding=2):\n    # internal body changed\n    val = float(amount) * float(rate)\n    return round(val, rounding)\n',
            encoding='utf-8',
        )
        heal_res = self.service.action({'action': 'memory_heal_skills', 'taskId': 'task-one', 'session': OWNER})
        self.assertEqual(1, heal_res['healedCount'])
        self.assertEqual('trial-used', heal_res['healedSkills'][0]['evidenceState'])
        self.assertEqual('feedback-verified', heal_res['healedSkills'][0]['priorEvidenceState'])
        self.assertEqual('compatible-unverified', heal_res['healedSkills'][0]['verificationStatus'])

        # Mere 'used' feedback must NOT promote a compatible-unverified skill back to feedback-verified
        view_calc = self.store.memory('task-one', 'calc_tax 세액 계산 절차')
        recall_calc_id = memory.record_recall(self.store, 'task-one', OWNER, 'req-calc-1', 'calc_tax 세액 계산 절차', view_calc['retrieval'])
        memory.feedback(self.store, 'task-one', OWNER, 'fb-used-only', recall_calc_id, sk_id, 'used', '단순 참조')
        sk_still_unver = memory.read_skill(self.store, 'task-one', sk_id)
        self.assertEqual('trial-used', sk_still_unver['evidence_state'])
        self.assertEqual('compatible-unverified', sk_still_unver['source'].get('verificationStatus'))

        # Re-verify via MCP tool `prism_memory_verify_skill` with failing proof -> degrades to needs_revision + synthesizes pitfall case
        mcp = PrismMemoryMCPServer(self.root, default_task='task-one', default_session=OWNER, standalone=False, store=self.store)
        ver_fail = mcp.call_tool('prism_memory_verify_skill', {
            'task': 'task-one',
            'skillId': sk_id,
            'executionProof': {'command': 'pytest test_calc.py', 'exitCode': 1, 'output': 'AssertionError: tax mismatch'},
        })
        self.assertFalse(ver_fail['ok'])
        self.assertEqual('needs_revision', ver_fail['evidenceState'])
        self.assertEqual('broken', ver_fail['verificationStatus'])
        self.assertEqual('case:pitfall-calc-tax-flow', ver_fail['synthesizedCaseId'])

        # 2. Clean-room data root resolution, Doctor, Backup, and Restore
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            custom_home = td_path / 'user_prism_home'
            fake_site_pkg = td_path / 'site-packages' / 'prism_memory'
            fake_site_pkg.mkdir(parents=True, exist_ok=True)
            resolved_root = resolve_default_data_root(
                package_dir=fake_site_pkg,
                env={'PRISM_MEMORY_HOME': str(custom_home)},
            )
            self.assertEqual(custom_home.resolve(), resolved_root)

            eng = MemoryEngine(custom_home, task='clean/task-1', session='clean-owner')
            eng.capture(
                '1. 클린룸 스냅샷 백업 매니페스트를 먼저 생성한다.\n2. 복원 후 SQLite 무결성 해시를 대조해 점검한다.',
                command='backup_test',
                force=True,
            )
            doc = eng.doctor()
            self.assertTrue(doc['ok'])
            self.assertTrue(doc['dataRootWritable'])
            self.assertEqual('wal', doc['sqlite']['journalMode'].lower())

            bak_dir = td_path / 'my_backup'
            bak_res = eng.backup(dest_dir=bak_dir)
            self.assertTrue(bak_res['ok'])
            self.assertTrue((bak_dir / 'backup_manifest.json').is_file())

            restore_target = td_path / 'restored_home'
            rest_res = restore_data_root(bak_dir, target_root=restore_target)
            self.assertTrue(rest_res['ok'])
            eng_restored = MemoryEngine(restore_target, task='clean/task-1', session='clean-owner')
            self.assertGreaterEqual(len(eng_restored.skills()['skills']), 1)

            # 2b. Clean-room read-only site-packages subprocess verification (no WorkStore, read-only package dir)
            ro_site = td_path / 'venv' / 'lib' / 'python3' / 'site-packages'
            ro_pkg = ro_site / 'prism_memory'
            ro_pkg.mkdir(parents=True, exist_ok=True)
            src_dir = Path(__file__).resolve().parent
            for mod_file in (src_dir / 'prism_memory').glob('*.py'):
                shutil.copy2(mod_file, ro_pkg / mod_file.name)
            for py_mod in ('work_memory.py', 'memory_vectors.py', 'memory_skills.py', 'memory_intelligence.py', 'memory_cli.py', 'prism_memory_mcp.py'):
                shutil.copy2(src_dir / py_mod, ro_site / py_mod)
            user_data_home = td_path / 'isolated_user_data'
            empty_cwd = td_path / 'empty_work_cwd'
            empty_cwd.mkdir(parents=True, exist_ok=True)
            # Make site-packages and prism_memory package directory read-only (0o555)
            for p_ro in [ro_pkg, ro_site]:
                p_ro.chmod(0o555)
            try:
                sub_env = {
                    **os.environ,
                    'PYTHONPATH': str(ro_site),
                    'PRISM_MEMORY_HOME': str(user_data_home),
                }
                sub_code = (
                    "import json; from prism_memory import MemoryEngine; "
                    "eng = MemoryEngine(task='wheel/test', session='wheel-user'); "
                    "doc = eng.doctor(); "
                    "cap = eng.capture('1. 클린룸 휠 패키지를 로드한다.\\n2. 읽기 전용 site-packages 밖의 데이터 루트에 기록한다.', command='wheel_check', force=True); "
                    "print(json.dumps({'ok': doc['ok'], 'inSitePkg': doc['installedInSitePackages'], 'separated': doc['packageAndDataSeparated'], 'skills': len(eng.skills()['skills'])}))"
                )
                proc_cr = subprocess.run(
                    [sys.executable, '-c', sub_code],
                    cwd=str(empty_cwd),
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                )
                self.assertEqual(0, proc_cr.returncode, msg=proc_cr.stderr)
                cr_out = json.loads(proc_cr.stdout.strip())
                self.assertTrue(cr_out['ok'])
                self.assertTrue(cr_out['inSitePkg'])
                self.assertTrue(cr_out['separated'])
                self.assertGreaterEqual(cr_out['skills'], 1)
            finally:
                for p_ro in [ro_site, ro_pkg]:
                    if p_ro.exists():
                        p_ro.chmod(0o755)

        # 3. Independent 4-Epoch x 4-Baseline x 4-Ablation Repo-Evolution Benchmark Suite
        bench_report = run_repo_evolution_benchmark()
        self.assertTrue(bench_report['ok'])
        self.assertEqual(4, bench_report['epochsEvaluated'])
        self.assertEqual(8, bench_report['systemsEvaluated'])
        self.assertTrue(bench_report['multiDbIsolation']['isolated'])

        prism_b = bench_report['baselines']['prism_v8_engine']
        static_b = bench_report['baselines']['static_agents_md']
        bm25_b = bench_report['baselines']['vanilla_bm25']
        no_ast_a = bench_report['ablations']['ablation_no_ast_guard']
        no_ver_a = bench_report['ablations']['ablation_no_execution_verification_gate']

        self.assertEqual(1.0, prism_b['taskSuccessRate'])
        self.assertEqual(0.0, prism_b['staleProcedurePoisoningRate'])
        self.assertEqual(0.0, prism_b['falseVerificationPromotionRate'])
        self.assertGreater(static_b['staleProcedurePoisoningRate'], 0.5)
        self.assertGreater(bm25_b['staleProcedurePoisoningRate'], 0.5)
        self.assertGreater(no_ast_a['staleProcedurePoisoningRate'], 0.5)
        self.assertGreater(no_ver_a['falseVerificationPromotionRate'], 0.5)
        self.assertGreater(bench_report['headlineComparison']['tokenSavingsFromDeltaPrompting'], 0.15)

    def test_v82_polyglot_anchors_zero_setup_audit_and_commit_bound_proof(self):
        """v8.2 P0 Product Upgrade Bundle:
        1) Polyglot structural contract anchors (TypeScript/JS functions, arrow functions, classes, interfaces + package.json scripts + Makefile targets + explicit resolution metadata).
        2) Zero-setup `deltaharness audit` (`audit_repository` + CLI/SDK/API/MCP) detecting missing_path, missing_script, missing_symbol, signature_drift, git-diff blast radius, and SARIF v2.1.0 / Markdown output.
        3) Commit-bound execution verification proof (`headSha`, `dirtyWorktree`, `testCount`) + rejection of 0-tests (`Ran 0 tests` / `0 passed`) and empty executionProof.
        """
        from prism_memory import (
            MemoryEngine,
            audit_repository,
            extract_file_symbol_hashes,
            extract_file_symbol_signatures,
            is_breaking_symbol_change,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td) / 'polyglot_repo'
            (repo_dir / 'src').mkdir(parents=True, exist_ok=True)

            # 1. Polyglot Anchor Extraction: TypeScript function, arrow function, class, interface
            ts_file = repo_dir / 'src' / 'billing.ts'
            ts_file.write_text(
                "export interface ChargeRequest {\n"
                "  accountId: string;\n"
                "  amountCents: number;\n"
                "  idempotencyKey?: string;\n"
                "}\n\n"
                "export async function chargeAccount(accountId: string, amountCents: number, currency: string = 'USD') {\n"
                "  return { ok: true, accountId, amountCents, currency };\n"
                "}\n\n"
                "export const formatInvoice = (invoiceId: string, locale: string = 'ko-KR') => {\n"
                "  return `${locale}:${invoiceId}`;\n"
                "};\n",
                encoding='utf-8',
            )
            ts_sigs_v1 = extract_file_symbol_signatures(ts_file)
            self.assertEqual('typescript', ts_sigs_v1['__language__'])
            self.assertEqual('contract', ts_sigs_v1['__resolution__'])
            self.assertIn('ChargeRequest', ts_sigs_v1['symbols'])
            self.assertIn('chargeAccount', ts_sigs_v1['symbols'])
            self.assertIn('formatInvoice', ts_sigs_v1['symbols'])
            self.assertEqual(2, len(ts_sigs_v1['symbols']['chargeAccount']['reqArgs']))
            self.assertEqual(3, len(ts_sigs_v1['symbols']['chargeAccount']['allArgs']))
            self.assertEqual(['accountId', 'amountCents'], ts_sigs_v1['symbols']['ChargeRequest']['reqArgs'])

            # Compatible TS change (add optional parameter & optional interface field)
            ts_file.write_text(
                "export interface ChargeRequest {\n"
                "  accountId: string;\n"
                "  amountCents: number;\n"
                "  idempotencyKey?: string;\n"
                "  memo?: string;\n"
                "}\n\n"
                "export async function chargeAccount(accountId: string, amountCents: number, currency: string = 'USD', retry: number = 1) {\n"
                "  return { ok: true, accountId, amountCents, currency, retry };\n"
                "}\n\n"
                "export const formatInvoice = (invoiceId: string, locale: string = 'ko-KR') => {\n"
                "  return `${locale}:${invoiceId}`;\n"
                "};\n",
                encoding='utf-8',
            )
            ts_sigs_v2 = extract_file_symbol_signatures(ts_file)
            brk_fn, _ = is_breaking_symbol_change(ts_sigs_v1['symbols']['chargeAccount'], ts_sigs_v2['symbols']['chargeAccount'])
            brk_if, _ = is_breaking_symbol_change(ts_sigs_v1['symbols']['ChargeRequest'], ts_sigs_v2['symbols']['ChargeRequest'])
            self.assertFalse(brk_fn)
            self.assertFalse(brk_if)

            # Breaking TS change (add required parameter & required interface field)
            ts_file.write_text(
                "export interface ChargeRequest {\n"
                "  accountId: string;\n"
                "  amountCents: number;\n"
                "  tenantId: string;\n"
                "}\n\n"
                "export async function chargeAccount(accountId: string, amountCents: number, tenantId: string) {\n"
                "  return { ok: true, accountId, amountCents, tenantId };\n"
                "}\n",
                encoding='utf-8',
            )
            ts_sigs_v3 = extract_file_symbol_signatures(ts_file)
            brk_fn_v3, reason_fn = is_breaking_symbol_change(ts_sigs_v2['symbols']['chargeAccount'], ts_sigs_v3['symbols']['chargeAccount'])
            brk_if_v3, reason_if = is_breaking_symbol_change(ts_sigs_v2['symbols']['ChargeRequest'], ts_sigs_v3['symbols']['ChargeRequest'])
            self.assertTrue(brk_fn_v3)
            self.assertIn('required_positional_added', reason_fn)
            self.assertTrue(brk_if_v3)
            self.assertIn('interface_required_field_added', reason_if)

            # package.json and Makefile contract extraction + unsupported language check
            pkg_json = repo_dir / 'package.json'
            pkg_json.write_text(json.dumps({'name': 'demo', 'scripts': {'build': 'tsc', 'test': 'vitest run'}}), encoding='utf-8')
            makefile = repo_dir / 'Makefile'
            makefile.write_text("build:\n\tnpm run build\n\ndeploy-staging:\n\techo deploying\n", encoding='utf-8')
            go_file = repo_dir / 'main.go'
            go_file.write_text("package main\nfunc main() {}\n", encoding='utf-8')

            pkg_sigs = extract_file_symbol_signatures(pkg_json)
            mk_sigs = extract_file_symbol_signatures(makefile)
            go_sigs = extract_file_symbol_signatures(go_file)
            self.assertIn('build', pkg_sigs['symbols'])
            self.assertIn('deploy-staging', mk_sigs['symbols'])
            self.assertEqual('unsupported', go_sigs['__resolution__'])

            # 2. Initialize a real Git repo in repo_dir to test zero-setup audit + git-diff blast radius
            subprocess.run(['git', 'init'], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=str(repo_dir), check=True, capture_output=True)

            # Write clean v1 code & CLAUDE.md
            ts_file.write_text(
                "export async function chargeAccount(accountId: string, amountCents: number) {\n"
                "  return { ok: true, accountId, amountCents };\n"
                "}\n",
                encoding='utf-8',
            )
            claude_md = repo_dir / 'CLAUDE.md'
            claude_md.write_text(
                "# Billing Rules\n"
                "1. Call `src/billing.ts` function `chargeAccount(accountId, amountCents)` to bill a user.\n"
                "2. Run `npm run test` and `make deploy-staging` before release.\n",
                encoding='utf-8',
            )
            subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(['git', 'commit', '-m', 'Initial clean repo'], cwd=str(repo_dir), check=True, capture_output=True)

            # Clean audit on v1 must pass with 0 errors
            clean_audit = audit_repository(repo_dir)
            self.assertTrue(clean_audit['ok'], msg=clean_audit)
            self.assertEqual(0, clean_audit['errorCount'])

            # Now introduce breaking code drift + broken doc references in working tree / commit 2
            ts_file.write_text(
                "export async function chargeAccount(accountId: string, amountCents: number, tenantId: string) {\n"
                "  return { ok: true, accountId, amountCents, tenantId };\n"
                "}\n",
                encoding='utf-8',
            )
            agents_md = repo_dir / 'AGENTS.md'
            agents_md.write_text(
                "# Agent Runbook\n"
                "- Legacy helper in `src/deleted_module.py` must be checked.\n"
                "- Run `npm run nonexistent-lint` and `make missing-target`.\n"
                "- Invoke `src/billing.ts::removedFunction` when refunding.\n"
                "- Go anchor `main.go::main` is tracked.\n",
                encoding='utf-8',
            )
            subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(['git', 'commit', '-m', 'Breaking v2 change'], cwd=str(repo_dir), check=True, capture_output=True)

            # Run zero-setup audit with --base HEAD~1 --head HEAD and SARIF output
            drift_audit = audit_repository(repo_dir, base_ref='HEAD~1', head_ref='HEAD', output_format='sarif')
            self.assertFalse(drift_audit['ok'])
            rule_counts = drift_audit['countsByRule']
            self.assertGreaterEqual(rule_counts.get('missing_path', 0), 1)
            self.assertGreaterEqual(rule_counts.get('missing_script', 0), 2)
            self.assertGreaterEqual(rule_counts.get('missing_symbol', 0), 1)
            self.assertGreaterEqual(rule_counts.get('signature_drift', 0), 1)
            self.assertGreaterEqual(rule_counts.get('unsupported_anchor', 0), 1)
            self.assertGreaterEqual(len(drift_audit['blastRadius']), 1)
            self.assertEqual('2.1.0', drift_audit['sarif']['version'])

            # Also verify Markdown output via MemoryEngine.audit() and MCP `prism_memory_audit`
            eng = MemoryEngine(repo_dir, task='poly/task-1', session='poly-owner')
            md_audit = eng.audit(repo_path=repo_dir, output_format='markdown')
            self.assertIn('Ieum Check · Procedure & Instruction Audit', md_audit['formatted'])

            # 3. Commit-bound execution verification proof (`headSha`, `testCount`) & 0-tests rejection
            cap = eng.capture(
                f"1. `{ts_file}` 파일의 `chargeAccount` 함수를 호출해 과금한다.\n2. 결과를 검증한다.",
                command='ts_billing_capture',
                force=True,
            )
            ts_skill_id = cap['skills'][0]['skillId']

            # 3a. Empty executionProof with exitCode=0 must be rejected with ValueError
            with self.assertRaises(ValueError):
                eng.verify_skill(ts_skill_id, execution_proof={'exitCode': 0})

            # 3b. Test runner output with `Ran 0 tests` must be rejected (exitCode flipped to 1, broken / needs_revision)
            ver_zero = eng.verify_skill(
                ts_skill_id,
                command='python3 -c "print(\'Ran 0 tests in 0.000s\\n\\nOK\')"',
            )
            self.assertFalse(ver_zero['ok'])
            self.assertEqual('needs_revision', ver_zero['evidenceState'])
            self.assertEqual('zero-tests-rejected', ver_zero['verificationMode'])
            self.assertTrue(ver_zero['verificationEvidence']['zeroTestsRejected'])
            self.assertEqual(0, ver_zero['verificationEvidence']['testCount'])

            # 3c. Real passing test runner output records `headSha` (40-char git SHA) and `testCount == 3`
            ver_pass = eng.verify_skill(
                ts_skill_id,
                command='python3 -c "print(\'3 passed in 0.04s\')"',
            )
            self.assertTrue(ver_pass['ok'])
            self.assertEqual('feedback-verified', ver_pass['evidenceState'])
            self.assertEqual(3, ver_pass['verificationEvidence']['testCount'])
            self.assertEqual(40, len(ver_pass['verificationEvidence']['headSha']))

    def test_v83_dual_track_packaging_viberescue_and_anti_slop(self):
        """Verify Left-Hand (OSS GitHub Action & Quickstart) and Right-Hand (VibeRescue & Anti-Slop) modules."""
        from prism_memory import (
            build_rescue_bundle,
            scan_anti_slop,
            lock_design_system,
        )

        impl_root = Path(__file__).resolve().parent
        self.assertTrue((impl_root / 'action.yml').exists())
        self.assertTrue((impl_root / '.github' / 'workflows' / 'deltaharness-audit.yml').exists())
        self.assertTrue((impl_root / 'README.md').exists())

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / 'stuck-vibe-app'
            repo_dir.mkdir(parents=True, exist_ok=True)

            # 1. Create a realistic stuck Vibe-Coded Next.js + Supabase project with P1/P3/P6 issues
            (repo_dir / 'package.json').write_text(
                json.dumps({
                    'name': 'stuck-vibe-app',
                    'scripts': {'dev': 'next dev', 'build': 'next build'},
                    'dependencies': {'next': '14.2.0', 'react': '18.3.0', '@supabase/supabase-js': '2.45.0'},
                }),
                encoding='utf-8',
            )
            # Competing lockfiles (npm + pnpm)
            (repo_dir / 'package-lock.json').write_text('{}', encoding='utf-8')
            (repo_dir / 'pnpm-lock.yaml').write_text('lockfileVersion: 6.0\n', encoding='utf-8')

            # .env.local has a real secret (must NEVER leak) and is missing STRIPE_WEBHOOK_SECRET
            secret_token = 'sk-proj-' + 'SUPER_SECRET_NEVER_LEAK_1234567890abcdef'
            (repo_dir / '.env.local').write_text(
                f'OPENAI_API_KEY={secret_token}\nNEXT_PUBLIC_SUPABASE_URL=https://example.supabase.co\n',
                encoding='utf-8',
            )
            (repo_dir / '.env.example').write_text(
                'OPENAI_API_KEY=\nNEXT_PUBLIC_SUPABASE_URL=\nSTRIPE_WEBHOOK_SECRET=\n',
                encoding='utf-8',
            )

            # Client component exposing SUPABASE_SERVICE_ROLE_KEY and AI Slop UI/Copy
            comp_dir = repo_dir / 'src' / 'components'
            comp_dir.mkdir(parents=True, exist_ok=True)
            (comp_dir / 'HeroCard.tsx').write_text(
                '"use client";\n'
                'export function HeroCard() {\n'
                '  const adminKey = process.env.SUPABASE_SERVICE_ROLE_KEY;\n'
                '  const missingHook = process.env.STRIPE_WEBHOOK_SECRET;\n'
                '  return (\n'
                '    <div className="bg-gradient-to-r from-purple-600 via-indigo-500 to-pink-500 shadow-2xl backdrop-blur">\n'
                '      <h1 style={{ fontSize: "13.5px" }}>🚀 혁신적인 솔루션을 통해 생산성을 극대화합니다</h1>\n'
                '      <button>✨ 시작하기</button>\n'
                '    </div>\n'
                '  );\n'
                '}\n',
                encoding='utf-8',
            )

            # SQL migration without ENABLE ROW LEVEL SECURITY
            mig_dir = repo_dir / 'supabase' / 'migrations'
            mig_dir.mkdir(parents=True, exist_ok=True)
            (mig_dir / '001_init.sql').write_text(
                'CREATE TABLE public.customer_invoices (id uuid primary key, amount int);\n',
                encoding='utf-8',
            )

            # Run VibeRescue bundler (must report ok=False, exitCode=1, readiness=BLOCKED when critical blockers exist)
            rescue = build_rescue_bundle(repo_dir, write_files=True, output_format='markdown')
            self.assertFalse(rescue['ok'])
            self.assertEqual(1, rescue['exitCode'])
            self.assertEqual('BLOCKED', rescue['readiness'])
            self.assertFalse(rescue['secretSafety']['rawEnvValuesIncluded'])
            self.assertTrue(rescue['secretSafety']['verifiedOutputClean'])
            # Ensure the secret value is NEVER present anywhere in JSON or Markdown
            serialized_rescue = json.dumps(rescue, ensure_ascii=False)
            self.assertNotIn(secret_token, serialized_rescue)
            self.assertNotIn(secret_token, rescue['rescueBriefMarkdown'])
            self.assertNotIn(secret_token, rescue['onePromptRescueMarkdown'])

            blocker_kinds = {b['kind'] for b in rescue['blockers']}
            if rescue['jsAnalysis']['mode'] == 'typescript-ast':
                self.assertIn('client_secret_exposure', blocker_kinds)
            else:
                # Without the TypeScript parser the client finding is reported as UNVERIFIED (never decided by heuristics).
                self.assertNotIn('client_secret_exposure', blocker_kinds)
                self.assertTrue(any('STRIPE_SECRET_KEY' in u['reason'] or 'heuristic' in u['reason'] for u in rescue['unverified']))
            self.assertIn('missing_env_key', blocker_kinds)
            self.assertIn('supabase_rls_or_auth_gap', blocker_kinds)
            self.assertIn('competing_lockfiles', blocker_kinds)
            self.assertTrue((repo_dir / 'RESCUE_BRIEF.md').exists())
            self.assertTrue((repo_dir / 'ONE_PROMPT_RESCUE.md').exists())

            # Run Anti-Slop scanner before locking design system (must report ok=False, exitCode=1 when slopScore >= 20)
            slop = scan_anti_slop(repo_dir, output_format='markdown')
            self.assertFalse(slop['ok'])
            self.assertEqual(1, slop['exitCode'])
            self.assertGreater(slop['slopScore'], 30)
            slop_cats = set(slop['byCategory'].keys())
            self.assertIn('missing_design_lock', slop_cats)
            self.assertIn('purple_radial_blob_slop', slop_cats)
            self.assertIn('fractional_font_slop', slop_cats)
            self.assertIn('emoji_chrome_slop', slop_cats)
            self.assertIn('korean_ai_copy_slop', slop_cats)

            # Lock Reference-First Design System and verify DESIGN.md & design-tokens.css
            lock_res = lock_design_system(repo_dir, archetype='linear-precision', write_files=True)
            self.assertTrue(lock_res['ok'])
            self.assertTrue((repo_dir / 'DESIGN.md').exists())
            self.assertTrue((repo_dir / 'design-tokens.css').exists())

            # Clean the component and re-scan -> must achieve 0 Slop Score (Grade A+, ok=True, exitCode=0)
            (comp_dir / 'HeroCard.tsx').write_text(
                '"use client";\n'
                'export function HeroCard() {\n'
                '  return (\n'
                '    <section className="surface-card">\n'
                '      <h1>막힌 배포 환경과 누락된 환경변수를 10초 만에 찾아내요</h1>\n'
                '      <button><span className="status-dot-sm" /> 진단 시작</button>\n'
                '    </section>\n'
                '  );\n'
                '}\n',
                encoding='utf-8',
            )
            clean_slop = scan_anti_slop(repo_dir)
            self.assertTrue(clean_slop['ok'])
            self.assertEqual(0, clean_slop['exitCode'])
            self.assertEqual(0, clean_slop['slopScore'])
            self.assertEqual('A+', clean_slop['grade'])

    def test_v84_astra_adversarial_edge_cases_and_standalone_service(self):
        """Verify all adversarial counterexamples from gpt-6-astra:high Round 1 Audit:
        1) Commented-out JS/TS code & Markdown HTML comments do not fool `audit_repository`.
        2) Monorepo `cd packages/core && pnpm test` context resolution & `.github/workflows/*.yml` resolution.
        3) Single-identifier arrow function (`export const id = x => x`) and React `memo`/`forwardRef` HOC extraction.
        4) Invalid `--base` Git ref emits `invalid_git_ref` (`ok=False`, `exitCode=1`); broken TS syntax emits `syntax_error`.
        5) `viberescue`: bracket env (`process.env["STRIPE_SECRET_KEY"]`), `import "server-only"`, multi-file SQL migrations,
           `supabase_rls_enabled_without_policy`, multiline `USING (\\n true \\n)`, empty directory (`NO_TARGETS_FOUND`, `exitCode=2`),
           and `ENV_UNVERIFIED` vs `STATIC_CHECKS_PASSED`.
        6) `anti_slop`: empty directory (`UNVERIFIED`, `exitCode=2`), `.js`/`.mjs` and `products/` scanning, comment stripping,
           CSS variable gradient resolution (`var(--blob)`), WCAG contrast check (`low_contrast_text`), 3+ nested bordered cards in HTML,
           attribute-order-independent viewport zoom block, `apple-minimal` preset, and `--lock` overwrite protection (`--force`).
        7) `StandaloneStore` + `StandaloneService` full drop-in execution without `WorkStore` or `config_state`.
        """
        from prism_memory import (
            audit_repository,
            build_rescue_bundle,
            scan_anti_slop,
            lock_design_system,
            extract_file_symbol_signatures,
        )
        from prism_memory.standalone import StandaloneService

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            # 1. Empty directory checks (must NEVER return CLEAN_READY or A+)
            empty_dir = base / 'empty_folder'
            empty_dir.mkdir(parents=True, exist_ok=True)
            empty_rescue = build_rescue_bundle(empty_dir)
            self.assertFalse(empty_rescue['ok'])
            self.assertEqual(2, empty_rescue['exitCode'])
            self.assertEqual('NO_TARGETS_FOUND', empty_rescue['readiness'])

            empty_slop = scan_anti_slop(empty_dir)
            self.assertFalse(empty_slop['ok'])
            self.assertEqual(2, empty_slop['exitCode'])
            self.assertEqual('UNVERIFIED', empty_slop['grade'])

            # 2. Commented-out JS/TS exports, HOC wrappers, single-identifier arrow functions, and broken syntax
            adv_repo = base / 'adv_repo'
            (adv_repo / 'packages' / 'core').mkdir(parents=True, exist_ok=True)
            (adv_repo / '.github' / 'workflows').mkdir(parents=True, exist_ok=True)
            (adv_repo / '.github' / 'workflows' / 'ci.yml').write_text('name: CI\n', encoding='utf-8')
            (adv_repo / 'package.json').write_text(json.dumps({'name': 'root', 'scripts': {'build': 'turbo build'}}), encoding='utf-8')
            (adv_repo / 'packages' / 'core' / 'package.json').write_text(
                json.dumps({'name': '@adv/core', 'scripts': {'test': 'vitest run'}}),
                encoding='utf-8',
            )

            ts_mod = adv_repo / 'packages' / 'core' / 'index.ts'
            ts_mod.write_text(
                "/*\n"
                "export function commentedOutGhost(a: string, b: number) { return a; }\n"
                "*/\n"
                "// export const anotherGhost = (x: number) => x;\n"
                "export const identity = x => x;\n"
                "export const MemoWidget = React.memo(function MemoWidget(props: { title: string }) {\n"
                "  return null;\n"
                "});\n",
                encoding='utf-8',
            )
            sigs = extract_file_symbol_signatures(ts_mod)
            self.assertTrue(sigs['__syntax_ok__'])
            self.assertNotIn('commentedOutGhost', sigs['symbols'])
            self.assertNotIn('anotherGhost', sigs['symbols'])
            self.assertIn('identity', sigs['symbols'])
            self.assertIn('MemoWidget', sigs['symbols'])

            # Monorepo AGENTS.md with HTML comments, `cd packages/core`, and workflow file reference
            (adv_repo / 'AGENTS.md').write_text(
                "# Monorepo Guide\n"
                "<!-- Ignore `src/nonexistent_in_comment.py` and `packages/core/index.ts::commentedOutGhost` -->\n"
                "1. Check workflow `ci.yml`.\n"
                "2. Run package tests:\n"
                "   `cd packages/core`\n"
                "   `pnpm test`\n"
                "3. Use `packages/core/index.ts::identity` and `packages/core/index.ts::MemoWidget`.\n",
                encoding='utf-8',
            )
            monorepo_audit = audit_repository(adv_repo)
            self.assertTrue(monorepo_audit['ok'], msg=monorepo_audit['findings'])
            self.assertEqual(0, monorepo_audit['errorCount'])

            # Reference the commented-out function -> must flag missing_symbol!
            (adv_repo / 'CLAUDE.md').write_text(
                "Call `packages/core/index.ts::commentedOutGhost`.\n",
                encoding='utf-8',
            )
            comment_audit = audit_repository(adv_repo)
            self.assertFalse(comment_audit['ok'])
            self.assertIn('missing_symbol', comment_audit['countsByRule'])

            # Broken TS syntax -> must flag syntax_error!
            broken_ts = adv_repo / 'packages' / 'core' / 'broken.ts'
            broken_ts.write_text("export function brokenFn(a: string {\n  return a;\n", encoding='utf-8')
            (adv_repo / 'CLAUDE.md').write_text("See `packages/core/broken.ts`.\n", encoding='utf-8')
            syn_audit = audit_repository(adv_repo)
            self.assertFalse(syn_audit['ok'])
            self.assertIn('syntax_error', syn_audit['countsByRule'])

            # Invalid Git base ref -> must flag invalid_git_ref!
            subprocess.run(['git', 'init'], cwd=str(adv_repo), check=True, capture_output=True)
            bad_ref_audit = audit_repository(adv_repo, base_ref='nonexistent-git-ref-9999')
            self.assertFalse(bad_ref_audit['ok'])
            self.assertIn('invalid_git_ref', bad_ref_audit['countsByRule'])

            # 3. VibeRescue adversarial edge cases
            vr_repo = base / 'vr_adv'
            (vr_repo / 'src').mkdir(parents=True, exist_ok=True)
            (vr_repo / 'supabase' / 'migrations').mkdir(parents=True, exist_ok=True)
            # Server-only module using secret -> must NOT be flagged as client exposure
            (vr_repo / 'src' / 'server_billing.ts').write_text(
                'import "server-only";\n'
                'export const key = process.env["STRIPE_SECRET_KEY"];\n',
                encoding='utf-8',
            )
            # Client module using bracket notation secret -> MUST be flagged as client_secret_exposure
            # Commented-out secret -> must NOT be flagged
            (vr_repo / 'src' / 'client_pay.tsx').write_text(
                '"use client";\n'
                '// const safeComment = process.env.OPENAI_API_KEY;\n'
                'export const leaked = process.env["STRIPE_SECRET_KEY"];\n',
                encoding='utf-8',
            )
            # Multi-file SQL migrations: table1 has RLS + policy across 2 files; table2 has RLS enabled but 0 policies; table3 has multiline USING (true)
            (vr_repo / 'supabase' / 'migrations' / '001_tables.sql').write_text(
                'CREATE TABLE public.orders (id uuid primary key, user_id uuid);\n'
                'CREATE TABLE public.audit_logs (id uuid primary key);\n'
                'CREATE TABLE public.profiles (id uuid primary key);\n',
                encoding='utf-8',
            )
            (vr_repo / 'supabase' / 'migrations' / '002_rls.sql').write_text(
                'ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;\n'
                'CREATE POLICY "orders_owner" ON public.orders FOR ALL USING (auth.uid() = user_id);\n'
                'ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;\n'
                'ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;\n'
                'CREATE POLICY "open_profiles" ON public.profiles FOR SELECT USING (\n  true\n);\n',
                encoding='utf-8',
            )
            vr_res = build_rescue_bundle(vr_repo)
            self.assertFalse(vr_res['ok'])
            vr_msgs = "\n".join(b['message'] for b in vr_res['blockers'])
            # orders must NOT be flagged because 002_rls.sql enables RLS and adds a valid policy
            self.assertNotIn('`orders`', vr_msgs)
            # audit_logs MUST be flagged for RLS enabled without policy
            self.assertIn('`audit_logs`', vr_msgs)
            # profiles MUST be flagged for multiline USING (true)
            self.assertIn('`profiles`', vr_msgs)
            # client_pay.tsx bracket notation MUST be flagged, server_billing.ts must NOT
            client_exposures = [b for b in vr_res['blockers'] if b['kind'] == 'client_secret_exposure']
            if vr_res['jsAnalysis']['mode'] == 'typescript-ast':
                self.assertEqual(1, len(client_exposures))
                self.assertIn('client_pay.tsx', client_exposures[0]['file'])
            else:
                self.assertEqual(0, len(client_exposures))
                unverified_files = {u['file'] for u in vr_res['unverified']}
                self.assertIn('src/client_pay.tsx', unverified_files)
                self.assertNotIn('src/server_billing.ts', unverified_files)

            # 4. Anti-Slop adversarial edge cases
            slop_repo = base / 'slop_adv'
            (slop_repo / 'products').mkdir(parents=True, exist_ok=True)
            # Empty DESIGN.md and comment-only design-tokens.css -> must flag missing_design_lock
            (slop_repo / 'DESIGN.md').write_text('', encoding='utf-8')
            (slop_repo / 'design-tokens.css').write_text('/* only comment */\n', encoding='utf-8')
            # CSS with var(--blob) purple and low WCAG contrast (#1E293B on #0F172A = 1.27:1)
            (slop_repo / 'styles.css').write_text(
                ':root { --blob: #8b5cf6; --bg-dark: #0F172A; --fg-dim: #1E293B; }\n'
                '.hero { background: radial-gradient(circle, var(--blob), transparent); }\n'
                '.btn { background-color: var(--bg-dark); color: var(--fg-dim); }\n',
                encoding='utf-8',
            )
            # HTML with 3 nested bordered cards and reversed-order viewport zoom block
            (slop_repo / 'products' / 'landing.html').write_text(
                '<!doctype html><html><head>\n'
                '<meta content="width=device-width, user-scalable=no, initial-scale=1" name="viewport">\n'
                '</head><body>\n'
                '<!-- 🚀 commented out emoji and 13.5px should be ignored -->\n'
                '<div class="card border"><div class="panel border"><div class="box border">Nested</div></div></div>\n'
                '</body></html>\n',
                encoding='utf-8',
            )
            adv_slop = scan_anti_slop(slop_repo)
            self.assertFalse(adv_slop['ok'])
            adv_cats = set(adv_slop['byCategory'].keys())
            self.assertIn('missing_design_lock', adv_cats)
            self.assertIn('purple_radial_blob_slop', adv_cats)
            self.assertIn('low_contrast_text', adv_cats)
            self.assertIn('nested_card_box_slop', adv_cats)
            self.assertIn('viewport_zoom_block', adv_cats)
            self.assertNotIn('emoji_chrome_slop', adv_cats)
            self.assertNotIn('fractional_font_slop', adv_cats)

            # Test apple-minimal preset and overwrite protection
            (slop_repo / 'DESIGN.md').write_text('# Custom User Design Lock\nDo not overwrite me without --force.\n', encoding='utf-8')
            (slop_repo / 'design-tokens.css').write_text(':root { --bg-canvas: #ffffff; --text-primary: #111111; }\n', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                lock_design_system(slop_repo, archetype='apple-minimal', write_files=True, force=False)
            self.assertIn('Custom User Design Lock', (slop_repo / 'DESIGN.md').read_text(encoding='utf-8'))

            forced_lock = lock_design_system(slop_repo, archetype='apple-minimal', write_files=True, force=True)
            self.assertTrue(forced_lock['ok'])
            self.assertTrue((slop_repo / 'DESIGN.md.bak').exists())
            self.assertIn('Apple Human Interface', (slop_repo / 'DESIGN.md').read_text(encoding='utf-8'))

            # 5. StandaloneService full smoke test
            svc = StandaloneService(base / 'standalone_svc_root')
            svc.store.create('prism-memory', 'Standalone Task', OWNER, goal='Standalone verification', task='task-sa')
            aug = svc.augment_turn(OWNER, '스탠드얼론 서비스 점검', '스탠드얼론 서비스 점검', 'req-sa-1')
            self.assertEqual('스탠드얼론 서비스 점검', svc.strip_memory_for_display(aug))
            st = svc.action({'action': 'memory_stats', 'taskId': 'task-sa'})
            self.assertTrue(st['ok'])


class Round4RegressionTests(unittest.TestCase):
    """Counterexamples from the Round 3 independent audit (gpt-6-astra). Synthetic inputs only."""

    def _repo(self, base: Path, files: dict) -> Path:
        for rel, content in files.items():
            f = base / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding='utf-8')
        return base

    def _git(self, repo: Path, *args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()

    def test_anonymous_default_export_and_line_metadata(self):
        from prism_memory.code_anchors import extract_file_symbol_signatures
        from prism_memory.audit import audit_repository
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), {
                'mod.ts': "export default function(a = ')') {\n  return a;\n}\n",
                'multi.ts': '// h\n\nexport function multi(\n  a: string,\n): string {\n  return a;\n}\n',
                'AGENTS.md': 'Use `mod.ts::default`.\n',
            })
            self.assertIn('default', extract_file_symbol_signatures(repo / 'mod.ts').get('symbols') or {})
            multi = (extract_file_symbol_signatures(repo / 'multi.ts').get('symbols') or {}).get('multi')
            self.assertEqual(3, multi.get('line'))
            rep = audit_repository(repo)
            self.assertFalse([f for f in rep['findings'] if f.get('ruleId') == 'missing_symbol'])

    def test_historical_anchor_reads_head_commit_not_worktree(self):
        from prism_memory.audit import audit_repository
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), {'mod.py': 'def real(a):\n    return a\n', 'AGENTS.md': 'Use `mod.py::real`.\n'})
            self._git(repo, 'init', '-q'); self._git(repo, 'config', 'user.email', 't@example.test'); self._git(repo, 'config', 'user.name', 'T')
            self._git(repo, 'add', '.'); self._git(repo, 'commit', '-qm', 'base'); base = self._git(repo, 'rev-parse', 'HEAD')
            (repo / 'mod.py').write_text('def real(a, b):\n    return a + b\n'); self._git(repo, 'add', '.'); self._git(repo, 'commit', '-qm', 'head'); head = self._git(repo, 'rev-parse', 'HEAD')
            count = lambda r: sum(1 for f in r['findings'] if f.get('ruleId') == 'signature_drift')
            committed = count(audit_repository(repo, base_ref=base, head_ref=head))
            (repo / 'mod.py').write_text('def real(a):\n    return a\n')
            dirty = count(audit_repository(repo, base_ref=base, head_ref=head))
            self.assertGreaterEqual(committed, 1)
            self.assertEqual(committed, dirty)

    def test_sql_string_and_comment_rls_do_not_count(self):
        from prism_memory.viberescue import build_rescue_bundle
        with tempfile.TemporaryDirectory() as td:
            fake = self._repo(Path(td) / 'fake', {'m.sql': "CREATE TABLE public.o(id uuid); SELECT 'ALTER TABLE public.o ENABLE ROW LEVEL SECURITY;';\n/* ALTER TABLE public.o ENABLE ROW LEVEL SECURITY; */\n"})
            self.assertNotEqual('STATIC_CHECKS_PASSED', build_rescue_bundle(fake)['summary']['readiness'])
            real = self._repo(Path(td) / 'real', {'m.sql': 'CREATE TABLE public.t(id uuid, user_id uuid);\nALTER TABLE public.t ENABLE ROW LEVEL SECURITY;\nCREATE POLICY own ON public.t FOR SELECT USING ((SELECT auth.uid()) = user_id);\n'})
            self.assertFalse([b for b in build_rescue_bundle(real)['blockers'] if 'rls' in str(b.get('ruleId', '')).lower()])

    def test_secret_masking_covers_tokens_without_false_positives(self):
        from prism_memory.viberescue import scrub_text_secrets
        for token in ('sk-proj-' + 'a' * 40, 'ghp_' + 'b' * 36, 'github_pat_' + 'c' * 60, 'AKIA' + 'D' * 16, 'xoxb-' + '123456789012-123456789012-' + 'e' * 24, 'postgres' + '://u:p@db.example.test/app'):
            self.assertNotIn(token, scrub_text_secrets('x ' + token + ' y'))
        for benign in ('task-20261017-abcdef0123456789', 'Run `desk-reviewer-2026-10-21.py`', 'pattern mysql://[^x]'):
            self.assertEqual(benign, scrub_text_secrets(benign))

    def test_git_porcelain_keeps_first_filename_character(self):
        from prism_memory.viberescue import build_rescue_bundle
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), {'README.md': 'a\n', 'app.js': 'export const a = 1;\n'})
            self._git(repo, 'init', '-q'); self._git(repo, 'config', 'user.email', 't@example.test'); self._git(repo, 'config', 'user.name', 'T')
            self._git(repo, 'add', '.'); self._git(repo, 'commit', '-qm', 'init')
            (repo / 'README.md').write_text('b\n')
            self.assertIn('README.md', build_rescue_bundle(repo)['git']['modifiedFiles'])

    def test_dynamic_github_actions_command_is_unverified_not_skipped(self):
        from prism_memory.audit import audit_repository
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), {'package.json': '{"scripts":{"test":"echo ok"}}', 'AGENTS.md': '```yaml\nrun: npm run ${{ matrix.script }}\nrun: npm run test\n```\n'})
            rep = audit_repository(repo)
            self.assertEqual(1, rep['unverifiedDynamicCommandCount'])
            self.assertGreaterEqual(rep['commandsChecked'], 1)
            self.assertEqual(0, rep['errorCount'])

    def test_opsec_fingerprint_canary_blocks_without_plaintext(self):
        import hashlib
        cli = Path(__file__).resolve().parent / 'memory_cli.py'
        canary = 'round4_unit_test_forbidden_identity'
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td) / 'repo', {'README.md': f'hello {canary}\n'})
            canon = Path(td) / 'canon.json'
            canon.write_text(json.dumps({'characters': {'t': {'forbidden_identifier_sha256_fingerprints': ['sha256:' + hashlib.sha256(canary.encode()).hexdigest()]}}}))
            r = subprocess.run([sys.executable, '-B', str(cli), 'opsec-check', '--repo', str(repo), '--canon', str(canon)], capture_output=True, text=True)
            self.assertEqual(1, r.returncode)
            self.assertNotIn(canary, r.stdout + r.stderr)
            canon.write_text(json.dumps({'characters': {'t': {'forbidden_identifier_sha256_fingerprints': ['sha256:nothex']}}}))
            r = subprocess.run([sys.executable, '-B', str(cli), 'opsec-check', '--repo', str(repo), '--canon', str(canon)], capture_output=True, text=True)
            self.assertEqual(2, r.returncode)

    def test_secret_bundle_with_github_pat_path_stays_clean(self):
        from prism_memory.viberescue import build_rescue_bundle
        token = 'github_pat_' + 'a' * 82
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), {'AGENTS.md': 'Use `src/' + token + '.py`.\n'})
            bundle = build_rescue_bundle(repo)
            self.assertNotIn(token, json.dumps(bundle))
            self.assertTrue(bundle['secretSafety']['verifiedOutputClean'])



class MemletInitWikiTests(unittest.TestCase):
    """`memlet init` / `memlet wiki`: zero-daemon onboarding and AST-anchored wiki status."""

    def _repo(self, base: Path) -> Path:
        (base / 'mod.py').write_text('def real(a):\n    return a\n\ndef body(a):\n    return a\n\ndef gone(a):\n    return a\n', encoding='utf-8')
        (base / 'AGENTS.md').write_text('# Rules\n- Use `mod.py::real`, `mod.py::body` and `mod.py::gone`.\n', encoding='utf-8')
        (base / 'CLAUDE.md').write_text('Call `mod.py::missing_one` first.\n', encoding='utf-8')
        (base / '.env').write_text('API_TOKEN=memlet-canary-do-not-read\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q'], cwd=base, check=True)
        subprocess.run(['git', '-c', 'user.email=t@example.test', '-c', 'user.name=T', 'add', '.'], cwd=base, check=True)
        subprocess.run(['git', '-c', 'user.email=t@example.test', '-c', 'user.name=T', 'commit', '-qm', 'base'], cwd=base, check=True)
        return base

    def _cli(self, *args):
        r = subprocess.run([sys.executable, '-B', CLI, *args], capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_memlet_init_dry_run_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            code, out = self._cli('init', str(repo), '--dry-run')
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertTrue(data['ok'])
            self.assertEqual(0, data['exitCode'])
            self.assertTrue(data['dryRun'])
            self.assertFalse((repo / '.memlet').exists())
            self.assertEqual(['AGENTS.md', 'CLAUDE.md'], data['instructionFiles'])
            self.assertRegex(data['repoHead'], r'^[0-9a-f]{40}$')
            self.assertEqual({'would-create'}, {a['op'] for a in data['actions']})
            self.assertEqual({'active': 3, 'compatible-unverified': 0, 'stale': 1, 'total': 4}, data['anchorSummary'])
            self.assertNotIn('memlet-canary-do-not-read', out)
            code2, out2 = self._cli('init', str(repo), '--dry-run')
            self.assertEqual(out, out2)  # deterministic
            code_md, md = self._cli('init', str(repo), '--dry-run', '--format', 'markdown')
            self.assertEqual(0, code_md)
            self.assertIn('## Memlet Init (dry run)', md)

    def test_memlet_init_creates_config_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            code, out = self._cli('init', str(repo))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertEqual({'create'}, {a['op'] for a in data['actions']})
            config = json.loads((repo / '.memlet' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual('memlet', config['tool'])
            self.assertEqual(data['repoHead'], config['repoHead'])
            self.assertEqual(['AGENTS.md', 'CLAUDE.md'], config['instructionFiles'])
            self.assertFalse(config['daemon'])
            index = (repo / '.memlet' / 'wiki' / 'INDEX.md').read_text(encoding='utf-8')
            self.assertIn('# Memlet Wiki Index', index)
            self.assertIn('<!-- memlet:anchor mod.py::real ast=', index)
            self.assertIn('<!-- memlet:status:begin -->', index)
            self.assertIn('| `stale` | `mod.py::missing_one` | missing_symbol |', index)
            for p in (repo / '.memlet').rglob('*'):
                if p.is_file():
                    self.assertNotIn(b'memlet-canary-do-not-read', p.read_bytes())
            # Re-running is idempotent and keeps human edits to INDEX.md.
            idx = repo / '.memlet' / 'wiki' / 'INDEX.md'
            idx.write_text(index + '\nHuman note.\n', encoding='utf-8')
            code, out = self._cli('init', str(repo))
            self.assertEqual(0, code)
            self.assertEqual({'unchanged', 'kept'}, {a['op'] for a in json.loads(out)['actions']})
            self.assertIn('Human note.', idx.read_text(encoding='utf-8'))

    def test_memlet_wiki_reports_ast_anchor_status(self):
        from prism_memory.memlet_wiki import wiki_status
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            # Before init: read-only preview, no files written.
            preview = wiki_status(repo)
            self.assertFalse(preview['initialized'])
            self.assertFalse((repo / '.memlet').exists())
            self.assertEqual(0, self._cli('init', str(repo))[0])
            code, out = self._cli('wiki', str(repo))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertTrue(data['initialized'])
            self.assertEqual(['.memlet/wiki/INDEX.md'], data['pages'])
            self.assertEqual(3, data['anchorSummary']['active'])
            # Drift the code: body-only change, signature change, deletion; plus a human-written page.
            (repo / 'mod.py').write_text('def real(a, b):\n    return a\n\ndef body(a):\n    return a + 1\n', encoding='utf-8')
            (repo / '.memlet' / 'wiki' / 'billing.md').write_text('# Billing\nSee `mod.py::body`.\n', encoding='utf-8')
            code, out = self._cli('wiki', str(repo))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            states = {a['anchor']: (a['status'], a['reason']) for a in data['anchors']}
            self.assertEqual(('compatible-unverified', 'body_changed_signature_compatible'), states['mod.py::body'])
            self.assertEqual(('stale', 'signature_drift'), states['mod.py::real'])
            self.assertEqual(('stale', 'missing_symbol'), states['mod.py::gone'])
            self.assertEqual(('stale', 'missing_symbol'), states['mod.py::missing_one'])
            self.assertIn('.memlet/wiki/billing.md', data['pages'])
            self.assertEqual({'op': 'update', 'path': '.memlet/wiki/INDEX.md'}, data['actions'][0])
            index = (repo / '.memlet' / 'wiki' / 'INDEX.md').read_text(encoding='utf-8')
            self.assertIn('| `stale` | `mod.py::real` | signature_drift |', index)
            self.assertIn('<!-- memlet:anchor mod.py::real ast=', index)  # pins are never auto-healed
            self.assertEqual(1, index.count('<!-- memlet:status:begin -->'))
            code, out = self._cli('wiki', str(repo), '--format', 'markdown')
            self.assertEqual(0, code)
            self.assertIn('## Memlet Wiki', out)
            self.assertIn('`stale`', out)
            self.assertEqual('unchanged', wiki_status(repo)['actions'][0]['op'])

    def test_memlet_ledger_records_user_edits_and_page_headers(self):
        from prism_memory.memlet_wiki import wiki_status
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            code, out = self._cli('init', str(repo))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertIn({'op': 'create', 'path': '.memlet/ledger.sqlite3'}, data['actions'])
            config = json.loads((repo / '.memlet' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual('.memlet/ledger.sqlite3', config['ledgerDb'])
            self.assertRegex(config['pinnedAnchors']['mod.py::real']['reqSigHash'], r'^[0-9a-f]{16}$')
            ledger_path = repo / '.memlet' / 'ledger.sqlite3'
            with contextlib.closing(sqlite3.connect(str(ledger_path))) as conn:
                snaps = dict(conn.execute('SELECT anchor, status FROM anchor_snapshots'))
                events = conn.execute('SELECT page, event_type FROM wiki_events').fetchall()
            self.assertEqual('active', snaps['mod.py::real'])
            self.assertEqual('stale', snaps['mod.py::missing_one'])
            self.assertEqual([('.memlet/wiki/INDEX.md', 'init')], events)
            # A second init with no changes leaves the ledger untouched.
            self.assertIn({'op': 'unchanged', 'path': '.memlet/ledger.sqlite3'}, json.loads(self._cli('init', str(repo))[1])['actions'])
            # Dry run reports would-be edits without touching the page or the ledger.
            page = repo / '.memlet' / 'wiki' / 'billing.md'
            page.write_text('# Billing\nSee `mod.py::body` and `mod.py::gone`.\n', encoding='utf-8')
            dry = wiki_status(repo, dry_run=True)
            self.assertFalse(dry['ledgerSynced'])
            self.assertEqual(['.memlet/wiki/billing.md'], dry['userEditedPages'])
            self.assertIn({'op': 'would-update', 'path': '.memlet/wiki/billing.md'}, dry['actions'])
            self.assertNotIn('memlet-page-status', page.read_text(encoding='utf-8'))
            # Real run: header injected after the title, user edit recorded, snapshots refreshed.
            (repo / 'mod.py').write_text('def real(a):\n    return a\n\ndef body(a):\n    return a + 1\n', encoding='utf-8')
            res = wiki_status(repo)
            self.assertTrue(res['ledgerSynced'])
            self.assertEqual(['.memlet/wiki/billing.md'], res['userEditedPages'])
            self.assertIn({'op': 'update', 'path': '.memlet/wiki/billing.md'}, res['actions'])
            text = page.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('# Billing\n<!-- memlet-page-status:start -->\n'
                                            '> **Memlet AST Status**: active=0, compatible-unverified=1, stale=1\n'
                                            '<!-- memlet-page-status:end -->\nSee '), text)
            self.assertNotIn('memlet-page-status', (repo / '.memlet' / 'wiki' / 'INDEX.md').read_text(encoding='utf-8'))
            with contextlib.closing(sqlite3.connect(str(ledger_path))) as conn:
                events = conn.execute('SELECT page, event_type FROM wiki_events ORDER BY id').fetchall()
                gone = conn.execute("SELECT status FROM anchor_snapshots WHERE anchor='mod.py::gone'").fetchone()
            self.assertEqual([('.memlet/wiki/INDEX.md', 'init'), ('.memlet/wiki/billing.md', 'user_edit')], events)
            self.assertEqual(('stale',), gone)
            # Rewriting only the generated header is not a user edit; a body edit is.
            again = wiki_status(repo)
            self.assertEqual([], again['userEditedPages'])
            self.assertIn({'op': 'unchanged', 'path': '.memlet/ledger.sqlite3'}, again['actions'])
            page.write_text(page.read_text(encoding='utf-8') + 'Refunds go through `mod.py::real`.\n', encoding='utf-8')
            edited = wiki_status(repo)
            self.assertEqual(['.memlet/wiki/billing.md'], edited['userEditedPages'])
            self.assertIn('active=1, compatible-unverified=1, stale=1', page.read_text(encoding='utf-8'))
            self.assertEqual(1, page.read_text(encoding='utf-8').count('<!-- memlet-page-status:start -->'))
            with contextlib.closing(sqlite3.connect(str(ledger_path))) as conn:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM wiki_events WHERE event_type='user_edit'").fetchone()[0])

    def test_memlet_optional_parameter_is_compatible_unverified(self):
        from prism_memory.memlet_wiki import wiki_status
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            self.assertEqual(0, self._cli('init', str(repo))[0])
            before = {a['anchor']: a for a in wiki_status(repo)['anchors']}
            self.assertEqual('active', before['mod.py::real']['status'])
            (repo / 'mod.py').write_text(
                'def real(a, optional=None):\n    return a\n\ndef body(a):\n    return a\n\ndef gone(a):\n    return a\n',
                encoding='utf-8')
            after = {a['anchor']: a for a in wiki_status(repo)['anchors']}
            real = after['mod.py::real']
            self.assertEqual(('compatible-unverified', 'optional_args_added_signature_compatible'), (real['status'], real['reason']))
            self.assertNotEqual(before['mod.py::real']['currentSigHash'], real['currentSigHash'])
            self.assertEqual(before['mod.py::real']['currentReqSigHash'], real['currentReqSigHash'])
            # Adding a required parameter is still signature drift.
            (repo / 'mod.py').write_text('def real(a, b, optional=None):\n    return a\n', encoding='utf-8')
            drifted = {a['anchor']: a for a in wiki_status(repo)['anchors']}
            self.assertEqual(('stale', 'signature_drift'), (drifted['mod.py::real']['status'], drifted['mod.py::real']['reason']))

    def test_memlet_check_markdown_cli_prints_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            code, out = self._cli('check', str(repo), '--format', 'markdown')
            self.assertEqual(1, code, out)  # `mod.py::missing_one` is a real error in the fixture
            self.assertTrue(out.startswith('## '), out)
            self.assertIn('`missing_symbol`', out)
            self.assertTrue(out.endswith('\n'))
            with self.assertRaises(json.JSONDecodeError):
                json.loads(out)
            res = memory.audit_repo(str(repo), output_format='markdown')
            self.assertIsInstance(res, dict)  # Python callers still get the structured report
            self.assertEqual(res['formatted'].splitlines()[0], out.splitlines()[0])  # same report; elapsed ms varies

    def test_memlet_check_repository_root_is_clean(self):
        res = memory.audit_repo(str(Path(__file__).resolve().parent))
        self.assertEqual(0, res['errorCount'], [f.get('message') for f in res.get('findings', [])])

    def _import_repo(self, base: Path) -> Path:
        (base / 'mod.py').write_text('def real(a):\n    return a\n\ndef route(task):\n    return task\n\ndef gone(a):\n    return a\n', encoding='utf-8')
        (base / 'CLAUDE.md').write_text('# Rules\n- Bill through `mod.py::real`.\n- Sessions live in `legacy_auth.py`.\n- Keep commits small.\n', encoding='utf-8')
        (base / '.cursorrules').write_text('Route work with `mod.py::route`.\nNever call `mod.py::gone` from handlers.\n', encoding='utf-8')
        (base / '.cursor' / 'rules').mkdir(parents=True)
        (base / '.cursor' / 'rules' / 'api.mdc').write_text('---\ndescription: api\nglobs: "*.py"\n---\n- Handlers call `mod.py::real`.\n', encoding='utf-8')
        (base / '.ai-memory' / 'notes').mkdir(parents=True)
        (base / '.ai-memory' / 'notes' / 'auth.md').write_text('# Auth\n- Tokens are minted in `mod.py::mint_token`.\n', encoding='utf-8')
        (base / '.env').write_text('API_TOKEN=memlet-canary-do-not-read\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q'], cwd=base, check=True)
        subprocess.run(['git', '-c', 'user.email=t@example.test', '-c', 'user.name=T', 'add', '.'], cwd=base, check=True)
        subprocess.run(['git', '-c', 'user.email=t@example.test', '-c', 'user.name=T', 'commit', '-qm', 'base'], cwd=base, check=True)
        return base

    def test_memlet_import_dry_run_tags_rules_with_live_ast_status(self):
        from prism_memory.memlet_wiki import import_rules
        with tempfile.TemporaryDirectory() as td:
            repo = self._import_repo(Path(td))
            code, out = self._cli('import', str(repo), '--dry-run')
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertFalse((repo / '.memlet').exists())
            self.assertEqual(['.ai-memory/notes/auth.md', '.cursor/rules/api.mdc', '.cursorrules', 'CLAUDE.md'], data['sourcesScanned'])
            self.assertEqual((7, 4, 0, 2, 1), (data['importedCount'], data['activeCount'], data['compatibleUnverifiedCount'],
                                               data['staleCount'], data['unanchoredCount']))
            self.assertEqual(1, data['brokenPathsCount'])  # `mod.py::mint_token` is a missing symbol, not a broken path
            self.assertEqual(['legacy_auth.py'], data['brokenPaths'])
            by_text = {r['text']: r for r in data['rules']}
            self.assertEqual('stale', by_text['Sessions live in `legacy_auth.py`.']['status'])
            self.assertEqual([{'anchor': 'legacy_auth.py', 'status': 'stale', 'reason': 'missing_path'}],
                             by_text['Sessions live in `legacy_auth.py`.']['anchors'])
            self.assertEqual('stale', by_text['Tokens are minted in `mod.py::mint_token`.']['status'])
            self.assertEqual('active', by_text['Handlers call `mod.py::real`.']['status'])  # .mdc front matter skipped
            self.assertEqual(5, by_text['Handlers call `mod.py::real`.']['line'])
            self.assertEqual('unanchored', by_text['Keep commits small.']['status'])
            self.assertNotIn('memlet-canary-do-not-read', out)
            self.assertEqual(out, self._cli('import', str(repo), '--dry-run')[1])  # deterministic
            only = import_rules(repo, source='cursorrules', dry_run=True)
            self.assertEqual(['.cursor/rules/api.mdc', '.cursorrules'], only['sourcesScanned'])
            self.assertEqual(['.ai-memory/notes/auth.md'], import_rules(repo, source='ai-memory', dry_run=True)['sourcesScanned'])
            self.assertEqual(['CLAUDE.md'], import_rules(repo, source='claude-md', dry_run=True)['sourcesScanned'])
            code, md = self._cli('import', str(repo), '--dry-run', '--format', 'markdown')
            self.assertEqual(0, code)
            self.assertTrue(md.startswith('## Memlet Import (dry run)'), md)
            self.assertIn('| `stale` | Sessions live in `legacy_auth.py`. | `CLAUDE.md:3` |', md)

    def test_memlet_import_writes_page_pins_and_ledger_then_detects_drift(self):
        from prism_memory.memlet_wiki import import_rules, wiki_status
        with tempfile.TemporaryDirectory() as td:
            repo = self._import_repo(Path(td))
            code, out = self._cli('import', str(repo), '--from', 'auto')
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertEqual({'create'}, {a['op'] for a in data['actions']})
            page = (repo / '.memlet' / 'wiki' / 'imported-rules.md').read_text(encoding='utf-8')
            self.assertIn('- `stale` Sessions live in `legacy_auth.py`. _(L3; `legacy_auth.py`: missing_path)_', page)
            self.assertIn('- `active` Bill through `mod.py::real`. _(L2)_', page)
            self.assertIn('<!-- memlet:anchor mod.py::real ast=', page)
            config = json.loads((repo / '.memlet' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual({'mod.py::gone', 'mod.py::real', 'mod.py::route'}, set(config['pinnedAnchors']))
            self.assertEqual('auto', config['imports']['from'])
            with contextlib.closing(sqlite3.connect(str(repo / '.memlet' / 'ledger.sqlite3'))) as conn:
                events = conn.execute('SELECT page, event_type FROM wiki_events').fetchall()
            self.assertEqual([('.memlet/wiki/imported-rules.md', 'import')], events)
            for p in (repo / '.memlet').rglob('*'):
                if p.is_file():
                    self.assertNotIn(b'memlet-canary-do-not-read', p.read_bytes())
            # `memlet wiki` adds its page header; re-import treats that as unchanged.
            wiki_status(repo)
            self.assertEqual('unchanged', import_rules(repo)['actions'][0]['op'])
            # Pins are reused: a required-arg change after import is stale, an optional arg is not.
            (repo / 'mod.py').write_text('def real(a, b):\n    return a\n\ndef route(task, effort="high"):\n    return task\n\ndef gone(a):\n    return a\n', encoding='utf-8')
            drift = {a['anchor']: (a['status'], a['reason']) for a in import_rules(repo, dry_run=True)['anchors']}
            self.assertEqual(('stale', 'signature_drift'), drift['mod.py::real'])
            self.assertEqual(('compatible-unverified', 'optional_args_added_signature_compatible'), drift['mod.py::route'])
            self.assertEqual(('active', 'ast_hash_match'), drift['mod.py::gone'])

    def test_memlet_handover_quarantines_stale_and_keeps_rejected(self):
        from prism_memory.memlet_wiki import AUTHORITY_BOUNDARY, build_handover, handover_signature
        with tempfile.TemporaryDirectory() as td:
            repo = self._import_repo(Path(td))
            self.assertEqual(0, self._cli('import', str(repo))[0])
            (repo / 'mod.py').write_text('def real(a):\n    return a\n\ndef route(task, effort="high"):\n    return task\n', encoding='utf-8')
            (repo / '.memlet' / 'wiki' / 'session.md').write_text(
                '# Session\n\n## Rejected Approaches\n- Patching `mod.py::real` with a retry decorator double-bills.\n\n'
                '## Dead-Ends\n- Mocking the gateway hid the idempotency bug.\n', encoding='utf-8')
            cap = build_handover(repo, next_step='Wire refunds through mod.py', rejected=['Do not vendor the SDK'])
            self.assertEqual(AUTHORITY_BOUNDARY, cap['authorityBoundary'])
            self.assertTrue(cap['authorityBoundary'].startswith('read-only-context-only'))
            self.assertRegex(cap['repoHead'], r'^[0-9a-f]{40}$')
            self.assertEqual(handover_signature(cap), cap['capsuleSignature'])
            self.assertRegex(cap['capsuleSignature'], r'^sha256:[0-9a-f]{64}$')
            active_text = ' '.join(a['rule'] for a in cap['activeConstraints'])
            self.assertIn('Bill through `mod.py::real`.', active_text)
            for dead in ('mod.py::gone', 'legacy_auth.py', 'mod.py::mint_token'):
                self.assertNotIn(dead, active_text)
            quarantined = {a['anchor']: a['reason'] for a in cap['quarantinedStaleAnchors']}
            self.assertEqual({'legacy_auth.py': 'missing_path', 'mod.py::gone': 'missing_symbol', 'mod.py::mint_token': 'missing_symbol'}, quarantined)
            self.assertEqual([{'anchor': 'mod.py::route', 'reason': 'optional_args_added_signature_compatible'}], cap['reverifyBeforeUse'])
            self.assertEqual(['Do not vendor the SDK', 'Patching `mod.py::real` with a retry decorator double-bills.',
                              'Mocking the gateway hid the idempotency bug.'], [r['approach'] for r in cap['rejectedApproaches']])
            self.assertEqual('Wire refunds through mod.py', cap['nextAction'])
            self.assertEqual(1, cap['executionProof']['modifiedFilesCount'])
            self.assertEqual({'active': 1, 'compatible-unverified': 1, 'stale': 3, 'total': 5}, cap['executionProof']['anchorHealth'])
            self.assertLess(cap['tokenEstimate'], 500)
            self.assertFalse((repo / '.memlet' / 'wiki' / 'HANDOVER.md').exists())
            self.assertEqual(cap, build_handover(repo, next_step='Wire refunds through mod.py', rejected=['Do not vendor the SDK']))
            # Tampering with a signed field breaks the signature.
            self.assertNotEqual(cap['capsuleSignature'], handover_signature(dict(cap, nextAction='rm -rf /')))

    def test_memlet_handover_markdown_cli_write_and_budget(self):
        from prism_memory.memlet_wiki import build_handover, estimate_tokens
        with tempfile.TemporaryDirectory() as td:
            repo = self._import_repo(Path(td))
            code, out = self._cli('handover', str(repo), '--next-step', 'Ship it', '--rejected', 'No retries in webhooks',
                                  '--format', 'markdown', '--write')
            self.assertEqual(0, code, out)
            self.assertTrue(out.startswith('## Routlet × Memlet Verified Handover Capsule\n'), out)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(out)
            self.assertIn('- Authority: read-only-context-only: execution and external side-effect approvals are NOT inherited', out)
            self.assertIn('### Quarantined stale anchors (not handed over)\n- `legacy_auth.py`: missing_path', out)
            self.assertIn('### Rejected approaches (do not retry)\n- No retries in webhooks', out)
            self.assertIn('### Next action\n- Ship it', out)
            self.assertLess(estimate_tokens(out), 500)
            handover_page = (repo / '.memlet' / 'wiki' / 'HANDOVER.md').read_text(encoding='utf-8')
            self.assertEqual('# Memlet Handover\n\n' + out, handover_page)
            with contextlib.closing(sqlite3.connect(str(repo / '.memlet' / 'ledger.sqlite3'))) as conn:
                self.assertEqual([('.memlet/wiki/HANDOVER.md', 'handover')], conn.execute('SELECT page, event_type FROM wiki_events').fetchall())
            # The written capsule is not re-read as rules or dead-ends, and .memlet/ files do not count as modified.
            again = build_handover(repo, next_step='Ship it', rejected=['No retries in webhooks'])
            self.assertEqual(['No retries in webhooks'], [r['approach'] for r in again['rejectedApproaches']])
            self.assertEqual(0, again['executionProof']['modifiedFilesCount'])
            # Many rules still fit the budget; trimmed items are counted, never silently dropped.
            (repo / 'AGENTS.md').write_text('# Rules\n' + ''.join(f'- Rule {i} keeps `mod.py::real` stable for caller group {i}.\n' for i in range(60)), encoding='utf-8')
            big = build_handover(repo, rejected=['No retries in webhooks'])
            self.assertLess(big['tokenEstimate'], 500)
            self.assertGreater(big['omitted']['activeConstraints'], 0)
            self.assertEqual(['No retries in webhooks'], [r['approach'] for r in big['rejectedApproaches']])

    def test_memlet_benchmark_compares_three_modes(self):
        from prism_memory.memlet_wiki import run_refactoring_comparison_benchmark
        res = run_refactoring_comparison_benchmark()
        modes = {m['mode']: m for m in res['modes']}
        self.assertEqual(['raw-markdown', 'unverified-wiki', 'memlet'], [m['mode'] for m in res['modes']])
        self.assertEqual(4, len(res['scenarios']))
        memlet = modes['memlet']
        self.assertEqual(0.0, memlet['staleProcedureInjectionRate'])
        self.assertEqual(0.0, memlet['optionalArgFalsePositiveRate'])
        self.assertEqual(1.0, memlet['handoverDeadEndPreventionRate'])
        self.assertEqual(3, memlet['staleProceduresTotal'])
        self.assertEqual(1.0, modes['raw-markdown']['staleProcedureInjectionRate'])
        self.assertEqual(0.0, modes['raw-markdown']['handoverDeadEndPreventionRate'])
        self.assertGreater(modes['unverified-wiki']['staleProcedureInjectionRate'], 0.0)
        self.assertLess(modes['unverified-wiki']['handoverDeadEndPreventionRate'], 1.0)
        for m in res['modes']:
            self.assertEqual(0, m['externalLlmCalls'])
            self.assertGreaterEqual(m['elapsedMs'], 0.0)
        self.assertEqual(res['resultDigest'], run_refactoring_comparison_benchmark()['resultDigest'])  # reproducible
        code, out = self._cli('benchmark', '.', '--format', 'markdown')
        self.assertEqual(0, code, out)
        self.assertIn('| Memlet | 0% (0/3) | 0% (0/1) | 100% (3/3) |', out)
        self.assertIn('| Raw Markdown | 100% (3/3) |', out)
        code, out = self._cli('compare-bench')
        self.assertEqual(0, code, out)
        self.assertEqual(res['resultDigest'], json.loads(out)['resultDigest'])

    def test_memlet_pack_generates_verified_stack_bundles_and_checksums(self):
        import hashlib
        from prism_memory.memlet_pack import PACK_FILES, PACK_STACKS, build_harness_pack, list_packs
        expected = set(PACK_FILES) | {'MANIFEST.json'}
        self.assertEqual(7, len(expected))
        listing = list_packs()
        self.assertEqual(['nextjs-supabase', 'fastapi-postgres', 'swift-ios'], listing['stacks'])
        for meta in listing['packs'].values():
            self.assertEqual(5, len(meta['pitfalls']))
        with self.assertRaises(ValueError):
            build_harness_pack('rails')

        # Dry run: everything in memory, nothing on disk, deterministic digest.
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / 'packs'
            dry = build_harness_pack('all', output_dir=str(out_dir), dry_run=True)
            self.assertFalse(dry['written'])
            self.assertFalse(out_dir.exists())
            self.assertEqual(21, dry['fileCount'])
            self.assertEqual(sum(p['totalBytes'] for p in dry['packs']), dry['totalBytes'])
            self.assertEqual(dry['manifestDigest'], build_harness_pack()['manifestDigest'])
            self.assertIn('| `swift-ios` | 7 |', dry['formatted'])
            code, out = self._cli('pack', '--dry-run', '--format', 'markdown')
            self.assertEqual(0, code, out)
            self.assertIn(dry['manifestDigest'], out)
            self.assertFalse(Path('nextjs-supabase').exists())

            # Real write via the CLI: 7 files per stack, MANIFEST checksums match the bytes on disk.
            code, out = self._cli('pack', '--output', str(out_dir))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertTrue(data['written'])
            self.assertEqual(dry['manifestDigest'], data['manifestDigest'])
            for stack in PACK_STACKS:
                pack_dir = out_dir / stack
                self.assertEqual(expected, {p.name for p in pack_dir.iterdir()})
                manifest = json.loads((pack_dir / 'MANIFEST.json').read_text(encoding='utf-8'))
                self.assertEqual('8.4.0', manifest['version'])
                self.assertEqual(stack, manifest['stack'])
                self.assertEqual(list(PACK_FILES), [f['path'] for f in manifest['files']])
                for entry in manifest['files']:
                    raw = (pack_dir / entry['path']).read_bytes()
                    self.assertEqual(entry['sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(entry['bytes'], len(raw))
                    self.assertNotIn(td.encode(), raw)
                self.assertTrue(manifest['quickstart'])
                pack_entry = next(p for p in data['packs'] if p['stack'] == stack)
                self.assertEqual(hashlib.sha256((pack_dir / 'MANIFEST.json').read_bytes()).hexdigest(), pack_entry['manifestSha256'])
                preset = json.loads((pack_dir / 'routlet-preset.json').read_text(encoding='utf-8'))
                self.assertEqual({'performance', 'balanced', 'efficiency'}, set(preset['options']))
                models = {preset['options'][o][r]['model'] for o in preset['options'] for r in ('plan', 'implement', 'review')}
                self.assertEqual({'Claude Opus 5.5', 'Fable 5.1', 'GPT-6 Astra', 'GPT-6.1 Sol'}, models)
                tokens = json.loads((pack_dir / 'anti-slop-tokens.json').read_text(encoding='utf-8'))
                self.assertEqual({'linear', 'stripe', 'toss'}, set(tokens['palettes']))
                for brand in tokens['palettes'].values():
                    self.assertEqual({'dark', 'light'}, set(brand))
                    for mode in brand.values():
                        self.assertTrue(all(r >= 4.5 for r in mode['contrast'].values()), mode['contrast'])
                constitution = (pack_dir / 'CLAUDE.md').read_text(encoding='utf-8')
                for heading in ('## AST Symbol Anchor Conventions', '## Session Delta Rules', '## Human Gates'):
                    self.assertIn(heading, constitution)
                self.assertIn('::', constitution)
                for vendor in ('Claude Code', 'Codex', 'Gemini', 'Cursor'):
                    self.assertIn(vendor, (pack_dir / 'AGENTS.md').read_text(encoding='utf-8'))
                skill = (pack_dir / 'SKILL.md').read_text(encoding='utf-8')
                for heading in ('## Pre-flight Checks', '## Execution Receipt', '## Rollback'):
                    self.assertIn(heading, skill)
            # Re-running is idempotent.
            code, out = self._cli('pack', '--output', str(out_dir), '--stack', 'swift-ios')
            self.assertEqual(0, code, out)
            self.assertEqual({'unchanged'}, {f['op'] for f in json.loads(out)['packs'][0]['files']})

            # PITFALLS.md dead-ends travel through `memlet handover` as rejected approaches.
            (Path(td) / 'repo').mkdir()
            repo = self._repo(Path(td) / 'repo')
            self.assertEqual(0, self._cli('init', str(repo))[0])
            (repo / '.memlet' / 'wiki' / 'pitfalls.md').write_bytes((out_dir / 'fastapi-postgres' / 'PITFALLS.md').read_bytes())
            code, out = self._cli('handover', str(repo))
            self.assertEqual(0, code, out)
            rejected = json.loads(out)['rejectedApproaches']
            self.assertEqual(5, sum(1 for r in rejected if r['approach'].startswith('[')))

    def test_memlet_ui_inspector_renders_self_contained_escaped_html(self):
        payloads = ('<script>alert("xss")</script>', '<img src=x onerror=alert(1)>', '<svg onload=alert(2)>')
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            (repo / 'AGENTS.md').write_text(
                '# Rules\n- Use `mod.py::real` ' + payloads[0] + ' and ' + payloads[1] + '.\n'
                '- Keep `mod.py::body` stable ' + payloads[2] + '\n', encoding='utf-8')
            self.assertEqual(0, self._cli('init', str(repo))[0])
            code, out = self._cli('handover', str(repo), '--rejected', '<script>alert(3)</script>', '--write')
            self.assertEqual(0, code, out)

            out_file = Path(td) / 'out' / 'inspector.html'
            code, out = self._cli('ui', str(repo), '--output', str(out_file))
            self.assertEqual(0, code, out)
            data = json.loads(out)
            self.assertTrue(data['ok'])
            self.assertTrue(data['written'])
            self.assertEqual(0, data['externalNetworkCalls'])
            html = out_file.read_text(encoding='utf-8')
            self.assertEqual(len(html.encode('utf-8')), data['htmlBytes'])
            for payload in payloads + ('<script>alert(3)</script>',):
                self.assertNotIn(payload, html)
            self.assertIn('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;', html)
            self.assertNotRegex(html, r'(?i)<(?:img|svg|iframe)\b[^>]*\bon\w+=')
            # Self-contained: no external scripts, styles or images.
            self.assertNotRegex(html, r'(?i)<(?:script|img|iframe)\b[^>]*\bsrc\s*=\s*["\']?(?:https?:)?//')
            self.assertNotRegex(html, r'(?i)<link\b[^>]*\bhref\s*=\s*["\']?(?:https?:)?//')
            # No host path leaks.
            self.assertNotIn('/Users/', html)
            self.assertNotIn(td, html)
            self.assertNotIn(str(Path(td).resolve()), html)
            self.assertNotIn('memlet-canary-do-not-read', html)
            # AST health, benchmark, handover and Routlet matrix sections.
            for section_id in ('t-health', 't-bench', 't-handover', 't-routing'):
                self.assertIn(f'id="{section_id}"', html)
            for text in ('AST Anchor Health', 'Benchmark', 'Handover Capsule', 'Routlet'):
                self.assertIn(text, html)
            for model in ('Claude Opus 5.5', 'Fable 5.1', 'Astra', 'Sol'):
                self.assertIn(model, html)

            # Default target is .memlet/inspector.html; `inspector` (without --task) is the same command, and
            # re-rendering an unchanged repository is byte-identical.
            default_file = repo / '.memlet' / 'inspector.html'
            code, out = self._cli('inspector', str(repo))
            self.assertEqual(0, code, out)
            self.assertTrue(json.loads(out)['written'])
            first = default_file.read_text(encoding='utf-8')
            self.assertIn('id="t-routing"', first)
            code, out = self._cli('ui', str(repo))
            self.assertEqual(0, code, out)
            self.assertEqual(first, default_file.read_text(encoding='utf-8'))

    def test_memlet_program_name_and_entrypoints(self):
        import memory_cli
        with patch.object(sys, 'argv', ['/usr/local/bin/memlet']):
            self.assertEqual('memlet', memory_cli._program_name())
        for legacy in ('ieum-memory', 'deltaharness', 'prism-memory'):
            with patch.object(sys, 'argv', ['/usr/local/bin/' + legacy]):
                self.assertEqual(legacy, memory_cli._program_name())
        pyproject = (Path(__file__).resolve().parent / 'pyproject.toml').read_text(encoding='utf-8')
        for line in ('memlet = "memory_cli:cli"', 'memlet-mcp = "prism_memory_mcp:main"', 'ieum-memory = "memory_cli:cli"',
                     'deltaharness = "memory_cli:cli"', 'prism-memory = "memory_cli:cli"', 'viberescue = ', 'anti-slop = '):
            self.assertIn(line, pyproject)
        code, out = self._cli('check', '--help')
        self.assertEqual(0, code)
        code, out = self._cli('--help')
        self.assertIn('init', out)
        self.assertIn('wiki', out)
        for command in ('import', 'handover', 'compare-bench', 'ui', 'pack'):
            self.assertIn(command, out)


if __name__ == '__main__':
    unittest.main(verbosity=2)








"""Zero-dependency StandaloneStore and high-level MemoryEngine SDK decoupled from PRISM WorkStore."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time

import work_memory as ledger
import memory_skills as skills_ledger
import memory_intelligence as memory


class StandaloneStore:
    """Lightweight SQLite store implementing the PRISM Memory storage interface without requiring WorkStore,
    mobile-bridge routes, or notification/orchestration tables.
    """

    def __init__(self, root_dir=None, db_filename: str = None, default_owner: str = "standalone-owner"):
        if root_dir is None:
            from .paths import resolve_default_data_root
            self.root = resolve_default_data_root()
        else:
            self.root = Path(root_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if db_filename:
            self.db_path = self.root / db_filename
        elif (self.root / "work.sqlite3").exists():
            self.db_path = self.root / "work.sqlite3"
        else:
            self.db_path = self.root / "prism_memory.sqlite3"
        self.default_owner = default_owner
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self.db(True) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT 'default',
                    owner TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 1,
                    body TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL DEFAULT 0
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS input_leases (
                    task_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    session TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_batches (
                    task TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY(task, operation)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    task TEXT NOT NULL,
                    session TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'responsible',
                    reason TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(task, session)
                )"""
            )
            ledger.ensure_schema(conn)
            skills_ledger.ensure_schema(conn)
            memory.ensure_schema(conn)

    @contextmanager
    def db(self, write: bool = False):
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            try:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except Exception:
                if write:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def row(self, record):
        if record is None:
            return None
        d = dict(record)
        if isinstance(d.get("body"), str):
            try:
                d["body"] = json.loads(d["body"] or "{}")
            except Exception:
                d["body"] = {}
        return d

    def _check_input_lease(self, db, task: str):
        row = db.execute(
            "SELECT token, expires_at FROM input_leases WHERE task_id=?",
            (task,),
        ).fetchone()
        if row and float(row["expires_at"] or 0.0) > time.time():
            raise ValueError("input delivery in progress; retry memory write after send finishes")

    def get(self, task_id: str):
        with self.db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("업무를 찾지 못했어요.")
            return self.row(row)

    def ensure_standalone_task(self, task_id: str, session: str = None, title: str = ""):
        project = task_id.split("/", 1)[0] if "/" in str(task_id) else "standalone"
        return self.ensure_task(task_id, title=title or task_id, owner=session or self.default_owner, project=project)

    def ensure_task(self, task_id: str, title: str = "", owner: str = None, project: str = "default", goal: str = ""):
        ledger._identifier(task_id, "task")
        eff_owner = owner or self.default_owner
        now = time.time()
        with self.db(True) as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing:
                return self.row(existing)
            body = {
                "goal": goal or title or f"Task {task_id}",
                "decisions": "",
                "constraints": "",
                "nextAction": "",
                "openIssues": "",
            }
            conn.execute(
                "INSERT INTO tasks (id, title, project, owner, state, revision, body, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, title or task_id, project, eff_owner, "active", 1, json.dumps(body, ensure_ascii=False), now, now),
            )
            if goal:
                ledger.record(
                    conn, task_id, 1, f"init-goal:{task_id}", "goal", "field:goal", "assert",
                    goal, {"kind": "explicit-memory-edit", "session": eff_owner, "certainty": "explicit-user"}, now,
                )
            return self.row(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def memory(self, task: str, query: str = "", budget: int = 6000, include_retrieval: bool = True, **options):
        if set(options) - {"at", "known_at", "before", "after", "shared", "federated", "limit"}:
            raise ValueError("invalid memory query options")
        index_result = None
        if include_retrieval and query and memory.vectors._MODEL is not None:
            index_result = memory.index_task(self, task)
        q_str = str(query or "")[:100000]
        with self.db() as db:
            row = db.execute("SELECT revision FROM tasks WHERE id=?", (task,)).fetchone()
            if not row:
                raise ValueError("업무를 찾지 못했어요.")
            retrieval = memory.retrieve(db, task, q_str[:2000], budget, root=self.root, **options) if include_retrieval else None
        if (index_result is None and retrieval is not None and query
                and memory.vectors._MODEL is not None
                and retrieval.get("semanticIndexedCandidates", 0) == 0
                and not retrieval.get("semanticError")):
            index_result = memory.index_task(self, task)
            with self.db() as db:
                retrieval = memory.retrieve(db, task, q_str[:2000], budget, root=self.root, **options)
        with self.db() as db:
            row = db.execute("SELECT revision FROM tasks WHERE id=?", (task,)).fetchone()
            if retrieval is not None:
                retrieval["queryOmittedChars"] = max(0, len(q_str) - 2000)
            at = memory.timestamp(options.get("at"), "at")
            known = memory.timestamp(options.get("known_at"), "known_at")
            active = ledger.current(
                db, task,
                kinds=list(ledger.FIELDS.values()) + ["fact", "experience", "case", "hypothesis"],
                at=at, known_at=known,
            )
            return {
                "version": 8,
                "taskId": task,
                "revision": row["revision"],
                "indexing": index_result,
                "current": [r for r in active if r["kind"] in ledger.FIELDS.values()],
                "facts": [r for r in active if r["kind"] == "fact"],
                "experiences": [r for r in active if r["kind"] == "experience"],
                "cases": [r for r in active if r["kind"] == "case"],
                "hypotheses": [r for r in active if r["kind"] == "hypothesis"],
                "skills": skills_ledger.snapshot_skills(db, task, at=at, known_at=known),
                "skillHistory": skills_ledger.skill_events(db, task, limit=20),
                "links": skills_ledger.get_links(db, task, limit=80),
                "history": ledger.history(db, task, 30),
                "retrieval": retrieval,
            }

    def skills(self, task: str, query: str = "", include_inactive: bool = False, **options):
        if set(options) - {"at", "known_at", "before", "after", "shared", "federated", "limit", "budget"}:
            raise ValueError("invalid skill query options")
        at = memory.timestamp(options.get("at"), "at")
        known = memory.timestamp(options.get("known_at"), "known_at")
        before = memory.timestamp(options.get("before"), "before")
        after = memory.timestamp(options.get("after"), "after")
        with self.db() as db:
            row = db.execute("SELECT revision FROM tasks WHERE id=?", (task,)).fetchone()
            if not row:
                raise ValueError("업무를 찾지 못했어요.")
            items = skills_ledger.snapshot_skills(
                db, task, at=at, known_at=known, before=before, after=after,
                shared=bool(options.get("shared", False)),
                include_inactive=bool(include_inactive), limit=options.get("limit", 200),
            )
            retrieval = skills_ledger.retrieve_skills(
                db, task, query=str(query or "")[:2000], budget=options.get("budget", 2400),
                limit=min(50, options.get("limit", 8)), root=self.root,
                at=at, known_at=known, before=before, after=after,
                shared=bool(options.get("shared", False)),
                federated=bool(options.get("federated", False)),
                semantic_config=memory.settings(self.root),
            ) if query else None
            return {
                "taskId": task,
                "revision": row["revision"],
                "skills": items,
                "history": skills_ledger.skill_events(db, task, limit=30),
                "retrieval": retrieval,
            }


class MemoryEngine:
    """High-level commercial SDK for PRISM Memory v8.0.
    Can operate standalone on any workspace path (`MemoryEngine('/path/to/dir')`) or wrap an existing `WorkStore`.
    """

    def __init__(
        self,
        root_or_store=None,
        task: str = "default-task",
        session: str = "standalone-owner",
        project: str = None,
        default_task: str = None,
        default_session: str = None,
    ):
        eff_task = default_task or task
        eff_session = default_session or session
        eff_project = project or (eff_task.split("/", 1)[0] if "/" in str(eff_task) else "default")
        if hasattr(root_or_store, "db") and hasattr(root_or_store, "root"):
            self.store = root_or_store
        else:
            self.store = StandaloneStore(root_or_store, default_owner=eff_session)
        self.task = eff_task
        self.session = eff_session
        self.project = eff_project
        if isinstance(self.store, StandaloneStore):
            self.store.ensure_task(eff_task, owner=eff_session, project=eff_project)

    def brief(self, query: str = "", budget: int = 4000, shared: bool = False):
        return self.store.memory(self.task, query=query, budget=budget, include_retrieval=bool(query), shared=shared)

    def recall(self, query: str = "", budget: int = 6000, limit: int = 8, session: str = None, shared: bool = False, federated: bool = False):
        res = self.store.memory(self.task, query=query, budget=budget, limit=limit, shared=shared, federated=federated)
        res["recallId"] = memory.record_recall(
            self.store, self.task, session or self.session, "sdk:" + str(time.time_ns()), query, res["retrieval"],
        )
        return res

    def capture(self, text_or_payload, command: str = None, session: str = None, **kwargs):
        if isinstance(text_or_payload, str):
            payload = {"text": text_or_payload, **kwargs}
            if command:
                payload["command"] = command
        else:
            payload = dict(text_or_payload)
            if command and "command" not in payload:
                payload["command"] = command
            payload.update(kwargs)
        return memory.auto_capture(self.store, self.task, session or self.session, payload)

    def learn(self, entries: list, operation_id: str, revision: int = None, session: str = None):
        if revision is None:
            with self.store.db() as db:
                t_row = db.execute("SELECT revision FROM tasks WHERE id=?", (self.task,)).fetchone()
                revision = int(t_row["revision"]) if t_row else 1
        return memory.learn(self.store, self.task, session or self.session, revision, operation_id, entries)

    def preflight(self, plan: str, files: list = None, action_type: str = None):
        return memory.preflight_check(self.store, self.task, plan=plan, files=files, action_type=action_type)

    def capsule(self, plan: str, files: list = None, worker: str = "subagent", budget: int = 1200):
        return memory.build_subagent_capsule(self.store, self.task, plan=plan, files=files, worker=worker, budget=budget)

    def heal_skills(self, session: str = None, dry_run: bool = False):
        return memory.heal_skills(self.store, self.task, session=session or self.session, dry_run=dry_run)

    def verify_skill(
        self,
        skill_id: str,
        command=None,
        execution_proof: dict = None,
        operation_id: str = None,
        timeout: float = 15.0,
        session: str = None,
    ):
        return memory.verify_skill(
            self.store,
            self.task,
            session or self.session,
            skill_id,
            command=command,
            execution_proof=execution_proof,
            operation_id=operation_id,
            timeout=timeout,
        )

    def doctor(self):
        return memory.doctor(self.store)

    def backup(self, dest_dir=None, include_models: bool = False):
        return memory.backup(self.store, dest_dir=dest_dir, include_models=include_models)

    def restore(self, backup_dir):
        return memory.restore(self.store, backup_dir=backup_dir)

    def repo_benchmark(self, work_dir=None):
        return memory.repo_benchmark(work_dir=work_dir)

    def audit(self, repo_path=None, base_ref: str = None, head_ref: str = "HEAD", output_format: str = "json"):
        return memory.audit_repo(
            repo_path or self.store.root,
            base_ref=base_ref,
            head_ref=head_ref,
            output_format=output_format,
        )

    def skills(self, query: str = "", include_inactive: bool = False):
        return self.store.skills(self.task, query=query, include_inactive=include_inactive)

    def read_skill(self, skill_id: str):
        return memory.read_skill(self.store, self.task, skill_id)

    def consolidate(self, similarity_threshold: float = 0.35, dry_run: bool = False):
        return memory.consolidate_memories(
            self.store, self.task, session=self.session,
            similarity_threshold=similarity_threshold, dry_run=dry_run,
        )

    def benchmark(self, budget: int = 6000):
        return memory.benchmark(self.store, self.task, budget=budget)

    def inspector(self, budget: int = 6000):
        return memory.inspector(self.store, self.task, budget=budget)



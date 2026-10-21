"""Clean-room package vs user-data path resolution, E5 model discovery, and cross-platform backup/restore."""
from __future__ import annotations
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
MODULE_ROOT = PACKAGE_DIR.parent


def is_installed_site_package(path: Path = MODULE_ROOT) -> bool:
    """Return True if path resides inside site-packages or dist-packages."""
    parts = {p.lower() for p in Path(path).resolve().parts}
    return bool(parts & {"site-packages", "dist-packages"})


def resolve_default_data_root(explicit_root=None, module_dir=None, cwd=None, *, package_dir=None, env=None) -> Path:
    """Resolve user data directory cleanly separated from read-only wheel installation paths.

    Priority:
    1. Explicit `explicit_root` argument
    2. `PRISM_MEMORY_HOME` environment variable
    3. `<cwd>/.prism-memory` if it already exists in the current working directory
    4. `module_dir` ONLY if it is a local workspace (not site-packages) AND already contains `state/work.sqlite3`
    5. `~/.prism-memory` (cross-platform user data home)
    """
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    env_map = env if isinstance(env, dict) else os.environ
    env_home = str(env_map.get("PRISM_MEMORY_HOME", "") or "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    work_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    local_dot_dir = work_cwd / ".prism-memory"
    if local_dot_dir.is_dir():
        return local_dot_dir
    eff_mod = module_dir or package_dir
    mod_dir = Path(eff_mod).resolve() if eff_mod else MODULE_ROOT
    if not is_installed_site_package(mod_dir) and (
        (mod_dir / "state" / "work.sqlite3").exists() or (mod_dir / "work_tasks.sqlite3").exists()
    ):
        return mod_dir
    return (Path.home() / ".prism-memory").resolve()


def resolve_embedding_model_dir(data_root=None, module_dir=None) -> Path:
    """Locate the local E5-small ONNX model directory across env, user data, user cache, and bundled paths."""
    env_model = os.environ.get("PRISM_MEMORY_MODEL_DIR", "").strip()
    if env_model:
        p = Path(env_model).expanduser().resolve()
        if p.is_dir():
            return p
    candidates = []
    if data_root:
        dr = Path(data_root).expanduser().resolve()
        candidates.append(dr / "models" / "e5-small")
        candidates.append(dr / "memory_models" / "e5-small")
    home = Path.home()
    candidates.append(home / ".prism-memory" / "models" / "e5-small")
    candidates.append(home / ".cache" / "prism-memory" / "models" / "e5-small")
    mod_dir = Path(module_dir).resolve() if module_dir else MODULE_ROOT
    candidates.append(mod_dir / "memory_models" / "e5-small")
    for cand in candidates:
        if cand.is_dir() and (cand / "manifest.json").is_file():
            return cand
    return candidates[-1]


def check_model_status(model_dir: Path, expected_revision: str = "761b726dd34fb83930e26aab4e9ac3899aa1fa78") -> dict:
    """Verify whether the local E5 ONNX model exists and passes SHA-256 manifest verification."""
    folder = Path(model_dir)
    if not folder.is_dir():
        return {
            "available": False,
            "integrityOk": False,
            "path": str(folder),
            "reason": "local_embedding_model_missing",
            "fallbackMode": "deterministic-lexical-bm25-ppr",
        }
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {
            "available": False,
            "integrityOk": False,
            "path": str(folder),
            "reason": "manifest_missing_or_symlink",
            "fallbackMode": "deterministic-lexical-bm25-ppr",
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("revision") != expected_revision:
            return {
                "available": False,
                "integrityOk": False,
                "path": str(folder),
                "reason": "embedding_revision_mismatch",
                "fallbackMode": "deterministic-lexical-bm25-ppr",
            }
        for name in ("tokenizer.json", "model_quantized.onnx", "config.json"):
            f_path = folder / name
            if not f_path.is_file() or f_path.is_symlink():
                return {
                    "available": False,
                    "integrityOk": False,
                    "path": str(folder),
                    "reason": f"missing_file:{name}",
                    "fallbackMode": "deterministic-lexical-bm25-ppr",
                }
            actual_sha = hashlib.sha256(f_path.read_bytes()).hexdigest()
            if actual_sha != manifest.get("files", {}).get(name, {}).get("sha256"):
                return {
                    "available": False,
                    "integrityOk": False,
                    "path": str(folder),
                    "reason": f"embedding_integrity_failed:{name}",
                    "fallbackMode": "deterministic-lexical-bm25-ppr",
                }
        return {
            "available": True,
            "integrityOk": True,
            "path": str(folder),
            "revision": expected_revision,
            "reason": "ok",
            "fallbackMode": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "integrityOk": False,
            "path": str(folder),
            "reason": f"manifest_error:{type(exc).__name__}",
            "fallbackMode": "deterministic-lexical-bm25-ppr",
        }


def _resolve_active_db_path(data_root: Path, explicit_db_path=None) -> Path:
    if explicit_db_path:
        return Path(explicit_db_path).expanduser().resolve()
    candidates = [
        data_root / "state" / "work.sqlite3",
        data_root / "work_tasks.sqlite3",
        data_root / "work.sqlite3",
        data_root / "prism_memory.sqlite3",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return candidates[0]


def doctor_environment(explicit_root=None, module_dir=None, *, data_root=None, db_path=None) -> dict:
    """Inspect installation, data path separation, SQLite WAL support, and E5 model availability."""
    mod_dir = Path(module_dir).resolve() if module_dir else MODULE_ROOT
    eff_root = explicit_root if explicit_root is not None else data_root
    resolved_root = resolve_default_data_root(explicit_root=eff_root, module_dir=mod_dir)
    writable = False
    try:
        resolved_root.mkdir(parents=True, exist_ok=True)
        probe = resolved_root / f".write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False

    model_dir = resolve_embedding_model_dir(data_root=resolved_root, module_dir=mod_dir)
    model_status = check_model_status(model_dir)
    active_db = _resolve_active_db_path(resolved_root, explicit_db_path=db_path)
    skills_dir = resolved_root / "memory_skills"
    separated = (resolved_root != mod_dir) or not is_installed_site_package(mod_dir)

    journal_mode = "unknown"
    integrity_ok = True
    try:
        probe_db = active_db if active_db.is_file() else (resolved_root / ".doctor_probe.sqlite3")
        created_temp_db = not active_db.is_file()
        with sqlite3.connect(str(probe_db), timeout=5.0) as conn:
            jm_row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            journal_mode = str(jm_row[0]).lower() if jm_row else "unknown"
            chk_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(chk_row and str(chk_row[0]).lower() == "ok")
        if created_temp_db:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(probe_db) + suffix)
                if p.exists():
                    p.unlink()
    except Exception:
        integrity_ok = False

    return {
        "ok": writable and integrity_ok,
        "platform": platform.platform(),
        "pythonVersion": platform.python_version(),
        "packageRoot": str(mod_dir),
        "installedInSitePackages": is_installed_site_package(mod_dir),
        "dataRoot": str(resolved_root),
        "dataRootWritable": writable,
        "packageAndDataSeparated": separated,
        "dbPath": str(active_db),
        "dbExists": active_db.is_file(),
        "sqlite": {
            "journalMode": journal_mode,
            "integrityOk": integrity_ok,
        },
        "skillsDir": str(skills_dir),
        "embeddingModel": model_status,
        "searchMode": "hybrid-e5-bm25-ppr" if model_status["available"] else "deterministic-lexical-bm25-ppr",
    }


def backup_data_root(
    data_root: Path,
    dest_dir: Path = None,
    *,
    db_path: Path = None,
    include_models: bool = False,
) -> dict:
    """Create a consistent SQLite online backup + skills snapshot in dest_dir (cross-platform safe)."""
    src_root = Path(data_root).expanduser().resolve()
    if dest_dir is None:
        dest_dir = src_root / "backups" / f"snapshot-{int(time.time())}"
    dst_root = Path(dest_dir).expanduser().resolve()
    if src_root == dst_root:
        raise ValueError("backup destination must differ from source data_root")
    dst_root.mkdir(parents=True, exist_ok=True)
    (dst_root / "state").mkdir(parents=True, exist_ok=True)

    active_src_db = _resolve_active_db_path(src_root, explicit_db_path=db_path)
    try:
        rel_db = active_src_db.relative_to(src_root)
    except ValueError:
        rel_db = Path("state") / active_src_db.name
    dst_db = dst_root / rel_db
    dst_db.parent.mkdir(parents=True, exist_ok=True)

    db_sha = ""
    if active_src_db.is_file():
        with sqlite3.connect(str(active_src_db)) as s_conn, sqlite3.connect(str(dst_db)) as d_conn:
            s_conn.backup(d_conn)
        db_sha = hashlib.sha256(dst_db.read_bytes()).hexdigest()

    src_skills = src_root / "memory_skills"
    dst_skills = dst_root / "memory_skills"
    copied_skills = 0
    if src_skills.is_dir():
        for p in sorted(src_skills.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(src_skills)
            target = dst_skills / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            copied_skills += 1

    copied_models = 0
    if include_models:
        src_models = src_root / "memory_models"
        dst_models = dst_root / "memory_models"
        if src_models.is_dir():
            for p in sorted(src_models.rglob("*")):
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.relative_to(src_models)
                target = dst_models / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                copied_models += 1

    manifest = {
        "schema": "prism-memory.backup.v1",
        "createdAt": round(time.time(), 3),
        "sourceRoot": str(src_root),
        "relativeDbPath": str(rel_db),
        "dbSha256": db_sha,
        "copiedSkillFiles": copied_skills,
        "copiedModelFiles": copied_models,
    }
    (dst_root / "backup_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "backupDir": str(dst_root),
        "dbBackedUp": bool(db_sha),
        "dbSha256": db_sha,
        "copiedSkillFiles": copied_skills,
        "copiedModelFiles": copied_models,
    }


def restore_data_root(backup_dir: Path, target_root: Path, *, target_db_path: Path = None) -> dict:
    """Restore a backup created by backup_data_root into target_root and verify SQLite integrity."""
    b_root = Path(backup_dir).expanduser().resolve()
    t_root = Path(target_root).expanduser().resolve()
    manifest_file = b_root / "backup_manifest.json"
    if not manifest_file.is_file():
        raise ValueError("backup_manifest.json not found in backup_dir")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    t_root.mkdir(parents=True, exist_ok=True)
    (t_root / "state").mkdir(parents=True, exist_ok=True)

    rel_db_str = manifest.get("relativeDbPath") or "state/work.sqlite3"
    b_db = b_root / rel_db_str
    if not b_db.is_file():
        b_db = _resolve_active_db_path(b_root)

    if target_db_path:
        t_db = Path(target_db_path).expanduser().resolve()
    else:
        t_db = t_root / rel_db_str
    t_db.parent.mkdir(parents=True, exist_ok=True)

    integrity_ok = True
    if b_db.is_file():
        with sqlite3.connect(str(b_db)) as s_conn, sqlite3.connect(str(t_db)) as d_conn:
            s_conn.backup(d_conn)
        with sqlite3.connect(str(t_db)) as check_conn:
            chk = check_conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(chk and chk[0] == "ok")
        if not integrity_ok:
            raise RuntimeError("restored SQLite database failed integrity_check")

    b_skills = b_root / "memory_skills"
    t_skills = t_root / "memory_skills"
    restored_skills = 0
    if b_skills.is_dir():
        for p in sorted(b_skills.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(b_skills)
            target = t_skills / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            restored_skills += 1

    return {
        "ok": True,
        "restoredRoot": str(t_root),
        "restoredDbPath": str(t_db),
        "integrityOk": integrity_ok,
        "restoredSkillFiles": restored_skills,
        "manifest": manifest,
    }

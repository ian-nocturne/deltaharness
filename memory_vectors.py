"""Pinned, offline multilingual E5 embeddings. No downloads or model API calls."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parent
MODEL = 'multilingual-e5-small:761b726dd34fb83930e26aab4e9ac3899aa1fa78:int8'
MAX_EMBED_CACHE = 1024
_lock = threading.RLock()
_runtime = None
_MODEL = None
_error = ''
_embed_cache = OrderedDict()
_cache_hits = 0
_cache_misses = 0


def _resolve_model_folder():
    try:
        from prism_memory.paths import resolve_embedding_model_dir
        resolved = resolve_embedding_model_dir(package_root=ROOT)
        if resolved is not None and resolved.is_dir():
            return resolved
    except Exception:
        pass
    return ROOT / 'memory_models/e5-small'


def _load():
    global _runtime, _MODEL, _error
    if _runtime is not None and _MODEL is not None:
        return _runtime
    folder = _resolve_model_folder()
    if not folder.is_dir():
        raise RuntimeError('local_embedding_model_missing')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['revision'] != MODEL.split(':')[1]:
        raise RuntimeError('embedding_revision_mismatch')
    for name in ('tokenizer.json', 'model_quantized.onnx', 'config.json'):
        path = folder / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][name]['sha256']:
            raise RuntimeError('embedding_integrity_failed')
    for candidate_rt in (ROOT / 'memory_runtime', folder.parent.parent / 'memory_runtime'):
        if candidate_rt.is_dir():
            runtime_path = str(candidate_rt)
            if runtime_path not in sys.path:
                sys.path.append(runtime_path)
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(str(folder / 'model_quantized.onnx'), options,
                                  providers=['CPUExecutionProvider'])
    tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))
    config = json.loads((folder / 'config.json').read_text())
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(pad_id=config.get('pad_token_id', 1), pad_token='<pad>')
    _runtime = (np, session, tokenizer)
    _MODEL = _runtime
    _error = ''
    return _runtime


def embed(texts, query=False):
    """Return normalized float32 vectors; only local CPU inference occurs, with LRU caching."""
    global _error, _cache_hits, _cache_misses
    if not texts:
        return []
    if len(texts) > 32 or any(not isinstance(t, str) or len(t) > 4000 for t in texts):
        raise ValueError('bounded embedding input required')
    q_flag = bool(query)
    with _lock:
        try:
            np, session, tokenizer = _load()
            results = [None] * len(texts)
            missing_indices = []
            missing_texts = []
            for idx, t in enumerate(texts):
                ckey = (q_flag, t)
                cached = _embed_cache.get(ckey)
                if cached is not None:
                    _embed_cache.move_to_end(ckey)
                    results[idx] = cached
                    _cache_hits += 1
                else:
                    missing_indices.append(idx)
                    missing_texts.append(t)
                    _cache_misses += 1
            if missing_texts:
                prefix = 'query: ' if q_flag else 'passage: '
                batches = tokenizer.encode_batch([prefix + t for t in missing_texts])
                ids = np.array([t.ids for t in batches], dtype=np.int64)
                mask = np.array([t.attention_mask for t in batches], dtype=np.int64)
                values = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
                output = session.run(None, {v.name: values[v.name] for v in session.get_inputs()})[0]
                if output.ndim == 3:
                    output = (output * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None].clip(min=1)
                output = output / np.linalg.norm(output, axis=1, keepdims=True).clip(min=1e-12)
                for m_idx, row in zip(missing_indices, output):
                    vec_bytes = row.astype('<f4').tobytes()
                    results[m_idx] = vec_bytes
                    ckey = (q_flag, texts[m_idx])
                    _embed_cache[ckey] = vec_bytes
                    if len(_embed_cache) > MAX_EMBED_CACHE:
                        _embed_cache.popitem(last=False)
            return results
        except Exception as exc:
            _error = type(exc).__name__ + ':' + str(exc)[:160]
            raise


def similarity(left, right):
    """Fast dot-product similarity on normalized little-endian float32 byte buffers."""
    if not left or not right or len(left) != len(right):
        return 0.0
    if _runtime is not None:
        np = _runtime[0]
        a = np.frombuffer(left, dtype='<f4')
        b = np.frombuffer(right, dtype='<f4')
        return float(np.dot(a, b))
    from array import array
    a = array('f'); a.frombytes(left)
    b = array('f'); b.frombytes(right)
    if sys.byteorder != 'little':
        a.byteswap(); b.byteswap()
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def batch_similarity(query_vec, vec_list):
    """Vectorized matrix-vector dot product for a list of float32 byte buffers."""
    if not query_vec or not vec_list:
        return []
    if _runtime is not None:
        np = _runtime[0]
        q = np.frombuffer(query_vec, dtype='<f4')
        q_bytes = len(query_vec)
        if all(v and len(v) == q_bytes for v in vec_list):
            mat = np.frombuffer(b''.join(vec_list), dtype='<f4').reshape(len(vec_list), len(q))
            return (mat @ q).astype(float).tolist()
    return [similarity(query_vec, v) for v in vec_list]


def status():
    total = _cache_hits + _cache_misses
    return {
        'model': MODEL,
        'localOnly': True,
        'loaded': _runtime is not None,
        'available': (ROOT / 'memory_models/e5-small/manifest.json').is_file(),
        'cacheSize': len(_embed_cache),
        'cacheHitRate': round(_cache_hits / total, 4) if total else 0.0,
        'error': _error,
    }


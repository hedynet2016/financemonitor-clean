#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_persist.py — 狀態檔跨部署持久化（2026-10-22）

問題：Render 免費版的容器檔案系統是臨時的，每次重新部署（含冷啟動重建）
      都會清空 sentiment_index_state.json，導致台指期月結算日的「當月模擬
      總盈虧」每次歸零重算。

做法：用 GitHub Contents API 把狀態檔同步到備份 repo（不觸發 Render 部署），
      與每日 09:00 的 render_backup.py 使用同一組憑證 GITHUB_PAT。
        - 開機：restore() 取遠端檔案，與本機「依日期合併、本機優先」後寫回
        - 寫檔：08:00 評分、11:30 命中後 push_async() 背景上傳（不阻塞推播）

環境變數：
  GITHUB_PAT / GITHUB_TOKEN   必要；需 owner/repo 的 contents:write
  STATE_BACKUP_REPO           預設 hedynet2016/workbuddy-backup
  STATE_BACKUP_DIR            遠端目錄，預設 state
  STATE_BACKUP_BRANCH         預設 main

設計要點：
  - 全部失敗皆靜默（僅寫 log），不影響推播主流程
  - push 有去重（同一份內容 60 秒內不重複上傳）與執行緒鎖
  - 只處理檔名結尾為 _state.json 的檔案，避免測試用的暫存檔被上傳
"""
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_REPO = 'hedynet2016/workbuddy-backup'
DEFAULT_DIR = 'state'
DEFAULT_BRANCH = 'main'
API_ROOT = 'https://api.github.com'

# 持久化白名單（副檔名前綴）——避免測試暫存檔被誤傳
_PERSIST_SUFFIX = '_state.json'

_lock = threading.Lock()
_last_push: Dict[str, Tuple[str, float]] = {}   # name -> (content_sha1, ts)
_restored = False


# ── 環境 ────────────────────────────────────────────────────────────
def _token() -> str:
    return (os.environ.get('GITHUB_PAT') or os.environ.get('GITHUB_TOKEN') or '').strip()


def _repo() -> str:
    return (os.environ.get('STATE_BACKUP_REPO') or DEFAULT_REPO).strip()


def _dir() -> str:
    return (os.environ.get('STATE_BACKUP_DIR') or DEFAULT_DIR).strip('/')


def _branch() -> str:
    return (os.environ.get('STATE_BACKUP_BRANCH') or DEFAULT_BRANCH).strip()


def available() -> bool:
    """是否具備持久化條件（有 token 且檔名符合白名單）"""
    return bool(_token())


def _eligible(path: str) -> bool:
    return os.path.basename(path).endswith(_PERSIST_SUFFIX)


# ── GitHub Contents API ─────────────────────────────────────────────
def _api(method: str, remote_path: str, data: Optional[dict] = None,
         timeout: int = 25) -> Tuple[Optional[dict], Optional[str]]:
    """回傳 (payload, error)；檔案不存在時回 (None, None)"""
    tok = _token()
    if not tok:
        return None, 'no_token'
    url = f'{API_ROOT}/repos/{_repo()}/contents/{remote_path}'
    headers = {
        'Authorization': f'token {tok}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'financemonitor-state-persist',
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        detail = e.read().decode('utf-8', errors='replace')[:300]
        return None, f'HTTP {e.code}: {detail}'
    except Exception as e:                     # 網路、timeout、JSON 解析
        return None, str(e)


def _remote_path(name: str) -> str:
    return f'{_dir()}/{name}' if _dir() else name


def repo_ok() -> Dict:
    """探測備份 repo 是否可用（token 權限／repo 是否存在／是否私有）"""
    tok = _token()
    if not tok:
        return {'ok': False, 'error': 'no_token'}
    url = f'{API_ROOT}/repos/{_repo()}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {tok}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'financemonitor-state-persist',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read())
        return {'ok': True, 'repo': d.get('full_name'),
                'private': d.get('private'),
                'permissions': d.get('permissions')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'http': e.code,
                'error': e.read().decode('utf-8', errors='replace')[:200]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def remote_meta(name: str) -> Dict:
    """遠端檔案資訊（唯讀診斷用）"""
    payload, err = _api('GET', f'{_remote_path(name)}?ref={_branch()}')
    if err:
        return {'ok': False, 'error': err}
    if not payload:
        return {'ok': True, 'exists': False}
    return {
        'ok': True, 'exists': True,
        'path': payload.get('path'),
        'size': payload.get('size'),
        'sha': (payload.get('sha') or '')[:10],
        'commit': (payload.get('commit') or {}).get('sha', '')[:10],
        'updated': ((payload.get('commit') or {}).get('committer') or {}).get('date'),
    }


# ── 上傳 / 下載 ────────────────────────────────────────────────────
def upload(path: str, name: Optional[str] = None, reason: str = '') -> Tuple[bool, str]:
    """把本機狀態檔上傳到備份 repo（覆蓋）"""
    if not available():
        return False, 'no_token'
    if not _eligible(path):
        return False, 'not_eligible'
    if not os.path.exists(path):
        return False, 'local_missing'
    name = name or os.path.basename(path)
    try:
        raw = open(path, 'rb').read()
    except Exception as e:
        return False, f'read_fail: {e}'
    if not raw.strip():
        return False, 'empty_file'

    meta, err = _api('GET', f'{_remote_path(name)}?ref={_branch()}')
    if err:
        return False, err
    sha = (meta or {}).get('sha')
    body = {
        'message': f'state: {name}{" (" + reason + ")" if reason else ""}',
        'content': base64.b64encode(raw).decode('utf-8'),
        'branch': _branch(),
    }
    if sha:
        body['sha'] = sha
    res, err = _api('PUT', _remote_path(name), body, timeout=30)
    if err:
        return False, err
    if isinstance(res, dict) and res.get('content'):
        logger.info('[state-persist] 已上傳 %s（%d bytes, %s）',
                    _remote_path(name), len(raw), reason or 'sync')
        return True, 'ok'
    return False, 'unexpected_response'


def download(name: str) -> Tuple[Optional[dict], str]:
    """下載遠端狀態檔並解析為 dict"""
    payload, err = _api('GET', f'{_remote_path(name)}?ref={_branch()}')
    if err:
        return None, err
    if not payload or 'content' not in payload:
        return None, 'not_found'
    try:
        raw = base64.b64decode(payload['content'])
        st = json.loads(raw.decode('utf-8'))
        return st if isinstance(st, dict) else None, 'ok'
    except Exception as e:
        return None, f'decode_fail: {e}'


# ── 合併 / 還原 ────────────────────────────────────────────────────
def merge_states(local: Optional[dict], remote: Optional[dict],
                 history_max: int = 60, signal_days: int = 30) -> dict:
    """依日期合併兩份狀態（本機優先），避免還原時蓋掉當日已寫入的紀錄"""
    local = local or {}
    remote = remote or {}
    out = {'signals': {}, 'history': []}

    sigs: Dict[str, dict] = {}
    for src in (remote.get('signals') or {}, local.get('signals') or {}):
        for k, v in src.items():
            sigs[k] = v
    out['signals'] = dict(sorted(sigs.items())[-signal_days:])

    hist: Dict[str, dict] = {}
    for src in (remote.get('history') or [], local.get('history') or []):
        for r in src:
            d = r.get('date')
            if d:
                hist[d] = r
    out['history'] = [hist[k] for k in sorted(hist)][-history_max:]
    return out


def restore(name: str = 'sentiment_index_state.json',
            path: Optional[str] = None,
            history_max: int = 60) -> Dict:
    """開機還原：取遠端狀態與本機合併後寫回（本機優先）"""
    global _restored
    p = path or name
    if not available():
        return {'ok': False, 'reason': 'no_token'}
    if not _eligible(p):
        return {'ok': False, 'reason': 'not_eligible'}

    local = None
    if os.path.exists(p):
        try:
            local = json.load(open(p, encoding='utf-8'))
        except Exception as e:
            logger.warning('[state-persist] 本機狀態檔解析失敗（視為空）：%s', e)
            local = None

    remote, err = download(name)
    if err != 'ok':
        logger.warning('[state-persist] 無法取得遠端 %s（%s），沿用本機狀態', name, err)
        return {'ok': False, 'reason': err,
                'local_history': len((local or {}).get('history') or [])}

    merged = merge_states(local, remote, history_max=history_max)
    before = len((local or {}).get('history') or [])
    after = len(merged.get('history') or [])
    changed = merged != (local or {})

    if changed:
        # 原子寫入，避免 telegram_bot 於同一時間讀到半截檔案
        tmp = f'{p}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:
            return {'ok': False, 'reason': f'write_fail: {e}'}

    _restored = True
    logger.info('[state-persist] 還原 %s：遠端 %d 筆 / 本機 %d 筆 → 合併 %d 筆',
                name, len((remote or {}).get('history') or []), before, after)
    return {'ok': True, 'remote_history': len((remote or {}).get('history') or []),
            'local_history': before, 'merged_history': after, 'changed': changed}


def push_async(path: str, name: Optional[str] = None, reason: str = '',
               dedupe_secs: int = 60) -> bool:
    """背景上傳（不阻塞推播流程）；同一內容於 dedupe_secs 內不重複上傳"""
    if not available() or not _eligible(path) or not os.path.exists(path):
        return False
    name = name or os.path.basename(path)
    try:
        raw = open(path, 'rb').read()
    except Exception:
        return False
    digest = str(hash(raw))
    prev = _last_push.get(name)
    if prev and prev[0] == digest and time.time() - prev[1] < dedupe_secs:
        return False

    def _worker():
        with _lock:
            ok, msg = upload(path, name=name, reason=reason)
        if ok:
            _last_push[name] = (digest, time.time())
        else:
            logger.warning('[state-persist] 上傳 %s 失敗：%s', name, msg)

    threading.Thread(target=_worker, daemon=True, name=f'state-push-{name}').start()
    return True


def status(name: str = 'sentiment_index_state.json',
           path: Optional[str] = None) -> Dict:
    """唯讀診斷：本機筆數、遠端檔案資訊、是否具備 token"""
    p = path or name
    info: Dict = {'token': bool(_token()), 'repo': _repo(), 'dir': _dir(),
                  'branch': _branch(), 'name': name,
                  'local_exists': os.path.exists(p)}
    if info['local_exists']:
        try:
            st = json.load(open(p, encoding='utf-8'))
            info['local_history'] = len(st.get('history') or [])
            info['local_signals'] = len(st.get('signals') or {})
            hist = st.get('history') or []
            info['first_date'] = hist[0].get('date') if hist else None
            info['last_date'] = hist[-1].get('date') if hist else None
        except Exception as e:
            info['local_error'] = str(e)
    if info['token']:
        info['repo_probe'] = repo_ok()
        info['remote'] = remote_meta(name)
    info['restored_this_process'] = _restored
    return info

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_persist.py — 狀態檔跨部署持久化（2026-10-22）

問題：Render 免費版的容器檔案系統是臨時的，每次重新部署（含冷啟動重建）
      都會清空 sentiment_index_state.json，導致台指期月結算日的「當月模擬
      總盈虧」每次歸零重算。

做法：把狀態檔同步到 GitHub（不觸發 Render 部署），開機還原、寫檔後上傳。
      後端依序嘗試，任一成功即記住：
        1. repo    —— Contents API，repo 預設 hedynet2016/workbuddy-backup，
                      不存在時自動建立（private）；額度寬、可讀寫整檔
        2. gist    —— Gist API（description 標記 financemonitor-state-store），
                      repo 無權限時的備援；同樣自動建立、以標記查找
      憑證沿用每日 09:00 備份的 GITHUB_PAT（或 GITHUB_TOKEN）。

環境變數：
  GITHUB_PAT / GITHUB_TOKEN   需 contents:write（classic: repo；fine-grained: Contents 讀寫）
  STATE_BACKUP_REPO           預設 hedynet2016/workbuddy-backup
  STATE_BACKUP_DIR            遠端目錄，預設 state
  STATE_BACKUP_BRANCH         預設 main

設計要點：
  - 全部失敗皆靜默（僅寫 log），不影響推播主流程
  - push 有去重（同一份內容 60 秒內不重複上傳）與執行緒鎖
  - 只處理檔名結尾為 _state.json 的檔案，避免測試暫存檔被上傳
  - 還原採「依日期合併、本機優先」，不會蓋掉當日已寫入的紀錄
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
GIST_MARKER = 'financemonitor-state-store'

_PERSIST_SUFFIX = '_state.json'   # 白名單：只同步 *_state.json

_lock = threading.Lock()
_last_push: Dict[str, Tuple[str, float]] = {}   # name -> (digest, ts)
_backend: Optional[str] = None                  # 'repo' | 'gist'
_repo_ready = False
_restored = False


# ── 環境 ────────────────────────────────────────────────────────────
def _token() -> str:
    return (os.environ.get('GITHUB_PAT') or os.environ.get('GITHUB_TOKEN') or '').strip()


def _repo() -> str:
    return (os.environ.get('STATE_BACKUP_REPO') or DEFAULT_REPO).strip()


def _repo_name() -> str:
    return _repo().split('/')[-1]


def _repo_owner() -> str:
    parts = _repo().split('/')
    return parts[0] if len(parts) > 1 else ''


def _dir() -> str:
    return (os.environ.get('STATE_BACKUP_DIR') or DEFAULT_DIR).strip('/')


def _branch() -> str:
    return (os.environ.get('STATE_BACKUP_BRANCH') or DEFAULT_BRANCH).strip()


def available() -> bool:
    return bool(_token())


def _eligible(path: str) -> bool:
    return os.path.basename(path).endswith(_PERSIST_SUFFIX)


def _remote_path(name: str) -> str:
    return f'{_dir()}/{name}' if _dir() else name


# ── HTTP 基礎 ───────────────────────────────────────────────────────
def _http(method: str, url: str, data: Optional[dict] = None,
          timeout: int = 25) -> Tuple[Optional[dict], Optional[str], int]:
    """回傳 (payload, error, http_code)；404 視為 (None, None, 404)"""
    tok = _token()
    if not tok:
        return None, 'no_token', 0
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
            raw = resp.read()
            return (json.loads(raw) if raw else {}), None, resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:200].replace('\n', ' ')
        if e.code == 404:
            return None, None, 404
        return None, f'HTTP {e.code}: {detail}', e.code
    except Exception as e:
        return None, str(e), 0


def _contents_url(name: str = '') -> str:
    base = f'{API_ROOT}/repos/{_repo()}/contents'
    path = _remote_path(name) if name else ''
    return f'{base}/{path}' if path else base


# ── 後端 1：repo Contents API ───────────────────────────────────────
def repo_ok() -> Dict:
    payload, err, code = _http('GET', f'{API_ROOT}/repos/{_repo()}')
    if err:
        return {'ok': False, 'http': code, 'error': err}
    return {'ok': True, 'repo': payload.get('full_name'),
            'private': payload.get('private'),
            'permissions': payload.get('permissions')}


def _ensure_repo() -> Tuple[bool, str]:
    """repo 不存在時自動建立（private，auto_init 產生 main 分支）"""
    global _repo_ready
    if _repo_ready:
        return True, 'ok'
    probe = repo_ok()
    if probe.get('ok'):
        _repo_ready = True
        return True, 'ok'
    if probe.get('http') != 404:
        return False, f"repo_unavailable: {probe.get('error')}"
    owner = _repo_owner()
    me, _e, _c = _http('GET', f'{API_ROOT}/user')
    if not me:
        return False, 'repo_create_fail: cannot_read_user'
    if owner and me.get('login', '').lower() != owner.lower():
        return False, f'repo_missing_and_owner_mismatch: {_repo()}'
    res, err, code = _http('POST', f'{API_ROOT}/user/repos', {
        'name': _repo_name(), 'private': True, 'auto_init': True,
        'description': 'financemonitor state backup (sentiment_index_state.json)',
    }, timeout=30)
    if err:
        return False, f'repo_create_fail: {err}'
    _repo_ready = True
    logger.info('[state-persist] 已自動建立備份 repo %s', _repo())
    return True, 'created'


def _upload_repo(name: str, raw: bytes, reason: str) -> Tuple[bool, str]:
    ok, msg = _ensure_repo()
    if not ok:
        return False, msg
    meta, err, _c = _http('GET', f'{_contents_url(name)}?ref={_branch()}')
    if err:
        return False, err
    sha = (meta or {}).get('sha') if isinstance(meta, dict) else None
    body = {
        'message': f'state: {name}{" (" + reason + ")" if reason else ""}',
        'content': base64.b64encode(raw).decode('utf-8'),
        'branch': _branch(),
    }
    if sha:
        body['sha'] = sha
    res, err, _c = _http('PUT', _contents_url(name), body, timeout=30)
    if err:
        return False, err
    if isinstance(res, dict) and res.get('content'):
        return True, 'repo'
    return False, 'repo_unexpected_response'


def _download_repo(name: str) -> Tuple[Optional[dict], str]:
    payload, err, _c = _http('GET', f'{_contents_url(name)}?ref={_branch()}')
    if err:
        return None, err
    if not payload or 'content' not in payload:
        return None, 'not_found'
    try:
        return json.loads(base64.b64decode(payload['content']).decode('utf-8')), 'ok'
    except Exception as e:
        return None, f'decode_fail: {e}'


# ── 後端 2：Gist（repo 無權限時的備援）──────────────────────────────
def _gist_find() -> Tuple[Optional[str], str]:
    """以 description 標記找出狀態 gist，回傳 (gist_id, err)"""
    payload, err, _c = _http('GET', f'{API_ROOT}/gists?per_page=100')
    if err:
        return None, err
    for g in payload or []:
        if (g.get('description') or '').strip() == GIST_MARKER:
            return g.get('id'), ''
    return None, 'not_found'


def _upload_gist(name: str, raw: bytes, reason: str) -> Tuple[bool, str]:
    text = raw.decode('utf-8', errors='replace')
    gid, err = _gist_find()
    if gid:
        res, err, _c = _http('PATCH', f'{API_ROOT}/gists/{gid}',
                             {'files': {name: {'content': text}}}, timeout=30)
    else:
        if err != 'not_found':
            return False, err
        res, err, _c = _http('POST', f'{API_ROOT}/gists', {
            'description': GIST_MARKER,
            'public': False,
            'files': {name: {'content': text}},
        }, timeout=30)
    if err:
        return False, err
    return (True, 'gist') if isinstance(res, dict) and res.get('id') else (False, 'gist_unexpected')


def _download_gist(name: str) -> Tuple[Optional[dict], str]:
    gid, err = _gist_find()
    if not gid:
        return None, err
    res, err, _c = _http('GET', f'{API_ROOT}/gists/{gid}')
    if err:
        return None, err
    try:
        files = (res or {}).get('files') or {}
        if name not in files:
            return None, 'not_found'
        content = files[name].get('content')
        if content is None and files[name].get('raw_url'):
            with urllib.request.urlopen(files[name]['raw_url'], timeout=25) as r:
                content = r.read().decode('utf-8', errors='replace')
        return json.loads(content), 'ok'
    except Exception as e:
        return None, f'decode_fail: {e}'


# ── 對外：上傳 / 下載 ──────────────────────────────────────────────
def _backend_order():
    return (['gist', 'repo'] if _backend == 'gist' else ['repo', 'gist'])


def upload(path: str, name: Optional[str] = None, reason: str = '') -> Tuple[bool, str]:
    """把本機狀態檔上傳到 GitHub（失敗自動換後端）"""
    global _backend
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

    errors = {}
    for be in _backend_order():
        ok, msg = (_upload_repo if be == 'repo' else _upload_gist)(name, raw, reason)
        if ok:
            _backend = msg
            logger.info('[state-persist] 已上傳 %s → %s（%d bytes, %s）',
                        name, msg, len(raw), reason or 'sync')
            return True, msg
        errors[be] = msg
    logger.warning('[state-persist] 上傳 %s 失敗：%s', name, errors)
    return False, json.dumps(errors, ensure_ascii=False)


def download(name: str) -> Tuple[Optional[dict], str]:
    """下載遠端狀態：raw（免憑證）→ repo → gist，回傳 (state, backend/err)"""
    global _backend
    errors = {}
    st, msg = _download_raw(name)
    if msg == 'raw':
        return st, 'raw'
    errors['raw'] = msg
    for be in _backend_order():
        st, msg = (_download_repo if be == 'repo' else _download_gist)(name)
        if msg == 'ok':
            _backend = be
            return st, be
        errors[be] = msg
    return None, json.dumps(errors, ensure_ascii=False)


# ── 後端 0：公開 repo 的 raw 檔（免憑證讀取）──────────────────────
def _raw_url(name: str) -> str:
    """預設從 financemonitor-clean 的 state-store 分支讀取（公開 repo 免 token）

    GitHub Actions 每日把 /api/state-export 的內容 commit 到該分支，
    Render 開機時直接讀回——不需要任何 PAT，也不會觸發 Render 重新部署。
    """
    url = os.environ.get('STATE_RAW_URL')
    if url:
        return url
    owner = _repo_owner() or 'hedynet2016'
    return (f'https://raw.githubusercontent.com/hedynet2016/financemonitor-clean'
            f'/state-store/{_dir()}/{name}')


def _download_raw(name: str) -> Tuple[Optional[dict], str]:
    url = _raw_url(name)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'financemonitor-state-persist',
                                                   'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode('utf-8')), 'raw'
    except urllib.error.HTTPError as e:
        return None, ('not_found' if e.code == 404 else f'HTTP {e.code}')
    except Exception as e:
        return None, str(e)


# ── 合併 / 還原 ────────────────────────────────────────────────────
def merge_states(local: Optional[dict], remote: Optional[dict],
                 history_max: int = 60, signal_days: int = 30) -> dict:
    """依日期合併兩份狀態（本機優先），避免還原蓋掉當日已寫入的紀錄"""
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
    """開機還原：取遠端狀態與本機合併後寫回（本機優先）

    不需 GITHUB_PAT：優先讀公開 repo 的 raw 檔（state-store 分支）。
    """
    global _restored
    p = path or name
    if not _eligible(p):
        return {'ok': False, 'reason': 'not_eligible'}

    local = None
    if os.path.exists(p):
        try:
            local = json.load(open(p, encoding='utf-8'))
        except Exception as e:
            logger.warning('[state-persist] 本機狀態檔解析失敗（視為空）：%s', e)
            local = None

    # [2026-10-22] 改為「全部來源取聯集」：raw 存在但內容為空時（state-store 尚未
    # 同步、或 bootstrap 空快照），舊寫法會用空資料遮蔽 repo 裡的真紀錄。
    sources = []
    raw_st, raw_msg = _download_raw(name)
    if raw_msg == 'raw' and raw_st is not None:
        sources.append(('raw', raw_st))
    errs = {'raw': raw_msg}
    if available():
        repo_st, repo_msg = _download_repo(name)
        errs['repo'] = repo_msg
        if repo_msg == 'ok' and repo_st is not None:
            sources.append(('repo', repo_st))
        gist_st, gist_msg = _download_gist(name)
        errs['gist'] = gist_msg
        if gist_st is not None:
            sources.append(('gist', gist_st))
    src = '+'.join(s for s, _ in sources) or json.dumps(errs, ensure_ascii=False)

    if not sources:
        logger.warning('[state-persist] 無法取得遠端 %s（%s），沿用本機狀態', name, src)
        return {'ok': False, 'reason': src,
                'local_history': len((local or {}).get('history') or [])}

    merged = local or {'signals': {}, 'history': []}
    remote_total = 0
    for _s, st in sources:
        remote_total += len((st or {}).get('history') or [])
        merged = merge_states(merged, st, history_max=history_max)
    before = len((local or {}).get('history') or [])
    after = len(merged.get('history') or [])
    changed = merged != (local or {})

    if changed:
        tmp = f'{p}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)          # 原子替換，避免同時讀到半截檔
        except Exception as e:
            return {'ok': False, 'reason': f'write_fail: {e}'}

    _restored = True
    logger.info('[state-persist] 還原 %s（%s）：遠端 %d 筆 / 本機 %d 筆 → 合併 %d 筆',
                name, src, remote_total, before, after)
    return {'ok': True, 'backend': src,
            'remote_history': remote_total,
            'local_history': before, 'merged_history': after, 'changed': changed}


def push_async(path: str, name: Optional[str] = None, reason: str = '',
               dedupe_secs: int = 60) -> bool:
    """背景上傳（不阻塞推播流程）；同內容於 dedupe_secs 內不重複上傳"""
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

    threading.Thread(target=_worker, daemon=True, name=f'state-push-{name}').start()
    return True


def _request_verbose(method: str, url: str,
                     timeout: int = 20) -> Tuple[Optional[dict], Dict, Optional[str], int]:
    """帶回應標頭的請求（診斷 PAT 用），回傳 (payload, headers, error, code)"""
    tok = _token()
    if not tok:
        return None, {}, 'no_token', 0
    headers = {
        'Authorization': f'token {tok}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'financemonitor-pat-check',
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return (json.loads(raw) if raw else {}), hdrs, None, resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:200].replace('\n', ' ')
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        return None, hdrs, f'HTTP {e.code}: {detail}', e.code
    except Exception as e:
        return None, {}, str(e), 0


def pat_diagnose(write_test: bool = False) -> Dict:
    """診斷 GITHUB_PAT 是否可用，逐項指出失敗原因（唯讀，write_test 才寫 probe）

    檢查順序對應常見故障：token 不存在 → token 失效/過期 → 缺 scope →
    repo 不存在或無權限 → 權限足夠但只有讀（無 push）→ 可寫。
    """
    tok = _token()
    out: Dict = {
        'token_present': bool(tok),
        'token_hint': (f'{tok[:4]}…{tok[-4:]} (len={len(tok)})' if len(tok) > 12
                       else ('set' if tok else '')),
        'token_source': ('GITHUB_PAT' if os.environ.get('GITHUB_PAT')
                         else 'GITHUB_TOKEN' if os.environ.get('GITHUB_TOKEN') else None),
        'repo': _repo(), 'branch': _branch(), 'dir': _dir(),
        'checks': [],
    }
    if not tok:
        out['verdict'] = 'no_token'
        out['advice'] = '尚未設定 GITHUB_PAT（Render → Environment 新增後重新部署）'
        return out

    checks = out['checks']

    # 1) token 身分（驗證是否失效/過期）
    me, hdrs, err, code = _request_verbose('GET', f'{API_ROOT}/user')
    scopes = hdrs.get('x-oauth-scopes')
    expiry = hdrs.get('github-authentication-token-expiration')
    if code == 200 and me:
        checks.append({'step': 'token_identity', 'ok': True,
                       'user': me.get('login'), 'type': me.get('type')})
    else:
        checks.append({'step': 'token_identity', 'ok': False,
                       'http': code, 'error': err})
        out['verdict'] = 'invalid_token' if code == 401 else 'token_check_failed'
        out['advice'] = ('Token 無效或已過期（GitHub 回 401 Bad credentials）'
                         '—請重新產生 PAT 並更新 Render 環境變數')
        out['token_expiry'] = expiry
        return out
    out['token_expiry'] = expiry
    out['scopes'] = scopes if scopes else '(無 scope 標頭，通常為 fine-grained token)'
    if expiry:
        checks.append({'step': 'token_expiry', 'ok': True, 'expires_at': expiry,
                       'note': '此欄位為 GitHub 回報的到期時間'})

    # 2) repo 讀取與權限
    repo = _repo()
    payload, _h, rerr, rcode = _request_verbose('GET', f'{API_ROOT}/repos/{repo}')
    if rcode == 200 and payload:
        perms = payload.get('permissions') or {}
        can_push = bool(perms.get('push'))
        checks.append({'step': 'repo_access', 'ok': True, 'repo': payload.get('full_name'),
                       'private': payload.get('private'), 'permissions': perms})
        if not can_push:
            out['verdict'] = 'read_only'
            out['advice'] = ('Token 可讀取 repo 但無寫入權限（permissions.push=false）'
                             '—classic 需 repo scope，fine-grained 需 Contents: Read and write')
            return out
    elif rcode == 404:
        checks.append({'step': 'repo_access', 'ok': False, 'http': 404, 'repo': repo,
                       'error': 'not_found_or_no_permission'})
        out['verdict'] = 'repo_not_found'
        out['advice'] = (f'Token 看不到 {repo}（repo 不存在，或 fine-grained token 未授權此 repo）'
                         '—請在 token 的 Repository access 加入該 repo，或確認 repo 名稱')
        return out
    else:
        checks.append({'step': 'repo_access', 'ok': False, 'http': rcode, 'error': rerr})
        out['verdict'] = 'repo_check_failed'
        out['advice'] = 'repo 檢查失敗（非 404/200），請看 checks 的 error'
        return out

    # 3) 實際寫入（可選）：PUT 再 DELETE 一個 probe 檔
    if write_test:
        probe = f'{_remote_path("_pat_check.json")}'
        body = json.dumps({'probe': 'pat-check',
                           'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}).encode()
        url = f'{API_ROOT}/repos/{repo}/contents/{probe}'
        headers = {'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
                   'User-Agent': 'financemonitor-pat-check'}
        import base64 as _b64
        req_body = json.dumps({'message': 'pat-check probe', 'branch': _branch(),
                               'content': _b64.b64encode(body).decode()}).encode()
        wreq = urllib.request.Request(url, data=req_body, headers=headers, method='PUT')
        try:
            with urllib.request.urlopen(wreq, timeout=25) as resp:
                put_ok = resp.status in (200, 201)
            checks.append({'step': 'write_test', 'ok': put_ok, 'file': probe})
            # 清掉 probe（失敗不影響結論）
            try:
                g2, _h2, _e2, _c2 = _request_verbose('GET', url + f'?ref={_branch()}')
                sha = (g2 or {}).get('sha')
                if sha:
                    del_body = json.dumps({'message': 'pat-check cleanup', 'sha': sha,
                                           'branch': _branch()}).encode()
                    dreq = urllib.request.Request(url, data=del_body, headers=headers, method='DELETE')
                    urllib.request.urlopen(dreq, timeout=25).read()
            except Exception:
                pass
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')[:160].replace('\n', ' ')
            checks.append({'step': 'write_test', 'ok': False, 'http': e.code, 'error': detail})
            out['verdict'] = 'write_denied'
            out['advice'] = ('寫入被拒（Contents API 403/404）—classic 需 repo scope；'
                             'fine-grained 需 Contents: Read and write')
            return out
        except Exception as e:
            checks.append({'step': 'write_test', 'ok': False, 'error': str(e)})
            out['verdict'] = 'write_check_failed'
            return out

    out['verdict'] = 'ok'
    out['advice'] = 'Token 可用，repo 可讀寫，每日 09:00 備份與狀態持久化應可正常運作'
    return out


def status(name: str = 'sentiment_index_state.json',
           path: Optional[str] = None) -> Dict:
    """唯讀診斷：本機筆數、兩個後端可用性"""
    p = path or name
    info: Dict = {'token': bool(_token()), 'repo': _repo(), 'dir': _dir(),
                  'branch': _branch(), 'name': name,
                  'local_exists': os.path.exists(p),
                  'backend_used': _backend,
                  'restored_this_process': _restored}
    if info['local_exists']:
        try:
            st = json.load(open(p, encoding='utf-8'))
            hist = st.get('history') or []
            info['local_history'] = len(hist)
            info['local_signals'] = len(st.get('signals') or {})
            info['first_date'] = hist[0].get('date') if hist else None
            info['last_date'] = hist[-1].get('date') if hist else None
        except Exception as e:
            info['local_error'] = str(e)
    info['raw_url'] = _raw_url(name)
    _rst, _rmsg = _download_raw(name)
    info['raw'] = {'ok': _rmsg == 'raw',
                   'history': len((_rst or {}).get('history') or []) if _rst else None,
                   'error': None if _rmsg == 'raw' else _rmsg}
    if info['token']:
        info['repo_probe'] = repo_ok()
        gid, gerr = _gist_find()
        info['gist'] = {'ok': bool(gid), 'id': (gid or '')[:8], 'error': gerr or None}
    return info

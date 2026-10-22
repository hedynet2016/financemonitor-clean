#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_pat.py — 驗證 GITHUB_PAT 是否可用（本機先行檢查，免上 Render）

用法：
    python scripts/check_pat.py ghp_xxxxxxxx          # 直接帶 token
    GITHUB_PAT=ghp_xxx python scripts/check_pat.py    # 由環境變數讀
    python scripts/check_pat.py ghp_xxx --write        # 額外做實際寫入測試

檢查項目：token 是否有效／是否過期／擁有哪些 scope／能否看到備份 repo／
          是否有寫入權限，最後給出結論與修正建議。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state_persist as sp  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    write_test = '--write' in sys.argv
    if args:
        os.environ['GITHUB_PAT'] = args[0].strip()

    info = sp.pat_diagnose(write_test=write_test)

    print('=' * 60)
    print('GITHUB_PAT 檢查')
    print('=' * 60)
    print(f"token       : {info.get('token_hint') or '(未設定)'}")
    print(f"來源        : {info.get('token_source') or '-'}")
    print(f"備份 repo   : {info.get('repo')} ({info.get('branch')})")
    if info.get('token_expiry'):
        print(f"到期時間    : {info['token_expiry']}")
    if info.get('scopes'):
        print(f"scopes      : {info['scopes']}")
    print('-' * 60)
    for c in info.get('checks', []):
        flag = 'OK  ' if c.get('ok') else 'FAIL'
        detail = {k: v for k, v in c.items() if k not in ('step', 'ok')}
        print(f"[{flag}] {c.get('step')}: {json.dumps(detail, ensure_ascii=False)}")
    print('-' * 60)
    print(f"結論        : {info.get('verdict')}")
    print(f"建議        : {info.get('advice') or '-'}")
    print('=' * 60)
    return 0 if info.get('verdict') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())

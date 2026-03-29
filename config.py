#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一配置加载 — 优先读 .env，兼容旧 config.json
"""

import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env（如果存在）
_env_path = Path(__file__).parent / '.env'
load_dotenv(_env_path)

_cfg_path = Path(__file__).parent / 'config.json'
_json_cfg = {}
if _cfg_path.exists():
    try:
        _json_cfg = json.loads(_cfg_path.read_text(encoding='utf-8'))
    except Exception:
        pass


def _get(env_key, json_path=None, default=None):
    """优先取环境变量，其次取 config.json 中的值"""
    val = os.environ.get(env_key)
    if val is not None:
        return val
    if json_path:
        obj = _json_cfg
        for key in json_path:
            obj = obj.get(key, {}) if isinstance(obj, dict) else {}
        if obj and not isinstance(obj, dict):
            return obj
    return default


# ─── Web ───────────────────────────────────────────────────────────────────────

WEB_HOST = _get('WEB_HOST', ['web', 'host'], '0.0.0.0')
WEB_PORT = int(_get('WEB_PORT', ['web', 'port'], 5000))
SECRET_KEY = _get('SECRET_KEY', ['web', 'secret_key'], '') or secrets.token_hex(32)
ADMIN_PASSWORD = _get('ADMIN_PASSWORD', ['web', 'admin_password'], '')
MP_LOGIN_TIMEOUT = int(_get('MP_LOGIN_TIMEOUT', ['web', 'mp_login_timeout'], 300))

# ─── WZ 主库 ──────────────────────────────────────────────────────────────────

WZ_DB = {
    'host': _get('WZ_DB_HOST', ['wz_db', 'host'], '127.0.0.1'),
    'port': int(_get('WZ_DB_PORT', ['wz_db', 'port'], 3306)),
    'user': _get('WZ_DB_USER', ['wz_db', 'user'], 'cj'),
    'password': _get('WZ_DB_PASSWORD', ['wz_db', 'password'], ''),
    'database': _get('WZ_DB_DATABASE', ['wz_db', 'database'], 'cj'),
    'charset': 'utf8mb4',
}

# ─── Discuz 论坛库 ────────────────────────────────────────────────────────────

DISCUZ_DB = {
    'host': _get('DISCUZ_DB_HOST', ['discuz_db', 'host'], '127.0.0.1'),
    'port': int(_get('DISCUZ_DB_PORT', ['discuz_db', 'port'], 3306)),
    'user': _get('DISCUZ_DB_USER', ['discuz_db', 'user'], '00077'),
    'password': _get('DISCUZ_DB_PASSWORD', ['discuz_db', 'password'], ''),
    'database': _get('DISCUZ_DB_DATABASE', ['discuz_db', 'database'], '00077'),
    'charset': 'utf8mb4',
}

# ─── 论坛发帖 ─────────────────────────────────────────────────────────────────

FORUM_FID = int(_get('FORUM_FID', ['forum', 'fid'], 2))
FORUM_AUTHOR = _get('FORUM_AUTHOR', ['forum', 'author'], '砂鱼')
FORUM_AUTHORID = int(_get('FORUM_AUTHORID', ['forum', 'authorid'], 4))

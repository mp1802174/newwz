#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号管理系统 - Flask Web 应用
"""

import datetime
import logging
import secrets
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import config as cfg
import database
import wechat

# ─── 初始化 ────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = cfg.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log', encoding='utf-8'),
    ],
)
log = logging.getLogger(__name__)

# 简易内存操作日志（最近 200 条）
_op_logs = []


def add_log(msg, level='info'):
    entry = {
        'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'level': level,
        'msg': msg,
    }
    _op_logs.insert(0, entry)
    if len(_op_logs) > 200:
        _op_logs.pop()
    getattr(log, level, log.info)(msg)


# ─── 后台认证与 CSRF ─────────────────────────────────────────────────────────────


def _is_admin_authenticated():
    return bool(session.get('admin_authenticated'))


def _ensure_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def _check_csrf():
    expected = session.get('csrf_token', '')
    actual = request.headers.get('X-CSRF-Token', '')
    return bool(expected and actual and secrets.compare_digest(actual, expected))


def admin_page_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not _is_admin_authenticated():
            return redirect(url_for('index'))
        return view_func(*args, **kwargs)

    return wrapper


def admin_api_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not _is_admin_authenticated():
            return jsonify(success=False, message='请先登录后台管理'), 401
        return view_func(*args, **kwargs)

    return wrapper


def csrf_protected(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and not _check_csrf():
            return jsonify(success=False, message='CSRF 校验失败，请刷新页面后重试'), 403
        return view_func(*args, **kwargs)

    return wrapper


# ─── 页面路由 ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if not _is_admin_authenticated():
        return render_template(
            'index.html',
            admin_authed=False,
            admin_password_configured=bool(cfg.ADMIN_PASSWORD),
        )

    try:
        stats = database.get_stats()
    except Exception as e:
        stats = {'total': '?', 'published': '?', 'pending': '?', 'accounts': '?'}
        add_log(f'获取统计信息失败: {e}', 'error')

    accounts = list(wechat._load_fakeid_cache().keys())
    auth_ok = wechat.is_authenticated()
    auth_time = wechat.load_auth().get('saved_at', '未知')

    return render_template(
        'index.html',
        admin_authed=True,
        admin_password_configured=bool(cfg.ADMIN_PASSWORD),
        csrf_token=_ensure_csrf_token(),
        stats=stats,
        accounts=accounts,
        auth_ok=auth_ok,
        auth_time=auth_time,
        logs=_op_logs[:50],
        login_session=wechat.get_login_session(),
    )


@app.route('/articles')
@admin_page_required
def articles():
    account = request.args.get('account') or None
    limit = request.args.get('limit', 100, type=int)
    try:
        rows = database.get_articles(limit=limit, account=account)
        for r in rows:
            if isinstance(r.get('publish_timestamp'), datetime.datetime):
                r['publish_timestamp'] = r['publish_timestamp'].strftime('%Y-%m-%d %H:%M')
            if isinstance(r.get('fetched_at'), datetime.datetime):
                r['fetched_at'] = r['fetched_at'].strftime('%Y-%m-%d %H:%M')
    except Exception as e:
        add_log(f'查询文章失败: {e}', 'error')
        rows = []

    accounts = list(wechat._load_fakeid_cache().keys())
    return render_template(
        'articles.html',
        articles=rows,
        accounts=accounts,
        current_account=account,
        limit=limit,
    )


# ─── 管理后台 API ───────────────────────────────────────────────────────────────

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    if not cfg.ADMIN_PASSWORD:
        return jsonify(success=False, message='服务端未配置 ADMIN_PASSWORD，无法开启后台'), 500

    data = request.get_json(silent=True) or {}
    password = str(data.get('password', ''))
    if not password:
        return jsonify(success=False, message='请输入后台密码'), 400

    if not secrets.compare_digest(password, cfg.ADMIN_PASSWORD):
        add_log('后台登录失败：密码错误', 'warning')
        return jsonify(success=False, message='后台密码错误'), 401

    session.clear()
    session['admin_authenticated'] = True
    session['admin_login_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _ensure_csrf_token()
    add_log('后台管理员已登录')
    return jsonify(success=True, message='后台登录成功')


@app.route('/api/admin/logout', methods=['POST'])
@admin_api_required
@csrf_protected
def api_admin_logout():
    session.clear()
    add_log('后台管理员已退出')
    return jsonify(success=True, message='已退出后台')


# ─── 业务 API ──────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
@admin_api_required
@csrf_protected
def api_login():
    """启动网页登录扫码会话"""
    try:
        login_session = wechat.start_web_login(timeout=cfg.MP_LOGIN_TIMEOUT)
        add_log('已启动网页登录扫码流程')
        return jsonify(success=True, message=login_session['message'], login=login_session)
    except Exception as e:
        add_log(f'启动网页登录失败: {e}', 'error')
        return jsonify(success=False, message=f'启动网页登录失败: {e}', login=wechat.get_login_session())


@app.route('/api/login_status')
@admin_api_required
def api_login_status():
    """返回当前网页登录扫码状态"""
    return jsonify(
        success=True,
        login=wechat.get_login_session(),
        auth=wechat.is_authenticated(),
        auth_time=wechat.load_auth().get('saved_at', '未知'),
    )


@app.route('/api/crawl', methods=['POST'])
@admin_api_required
@csrf_protected
def api_crawl():
    """立即抓取所有公众号文章并入库"""
    data = request.get_json(silent=True) or {}
    limit = data.get('limit', 10)

    if not wechat.is_authenticated():
        return jsonify(success=False, message='尚未登录微信，请先扫码登录')

    try:
        add_log(f'开始抓取，每个公众号最多 {limit} 篇...')
        articles = wechat.crawl_all(limit_per_account=limit)
        result = database.save_articles(articles)
        msg = (
            f'抓取完成，获取 {len(articles)} 篇，'
            f'新入库 {result["inserted"]} 篇，补全正文 {result["updated"]} 篇'
        )
        add_log(msg)
        return jsonify(success=True, message=msg, fetched=len(articles), **result)

    except PermissionError as e:
        add_log(str(e), 'warning')
        return jsonify(success=False, message=str(e), need_login=True)
    except Exception as e:
        add_log(f'抓取失败: {e}', 'error')
        return jsonify(success=False, message=f'抓取失败: {e}')


@app.route('/api/crawl_account', methods=['POST'])
@admin_api_required
@csrf_protected
def api_crawl_account():
    """抓取指定单个公众号的文章"""
    data = request.get_json(silent=True) or {}
    account = data.get('account', '').strip()
    limit = data.get('limit', 10)

    if not account:
        return jsonify(success=False, message='请提供公众号名称')
    if not wechat.is_authenticated():
        return jsonify(success=False, message='尚未登录微信，请先扫码登录')

    try:
        add_log(f'抓取公众号：{account}')
        articles = wechat.get_articles(account, limit=limit)
        result = database.save_articles(articles)
        msg = (
            f'[{account}] 获取 {len(articles)} 篇，'
            f'新入库 {result["inserted"]} 篇，补全正文 {result["updated"]} 篇'
        )
        add_log(msg)
        return jsonify(success=True, message=msg, fetched=len(articles), **result)

    except PermissionError as e:
        add_log(str(e), 'warning')
        return jsonify(success=False, message=str(e), need_login=True)
    except ValueError as e:
        add_log(str(e), 'warning')
        return jsonify(success=False, message=str(e))
    except Exception as e:
        add_log(f'抓取失败: {e}', 'error')
        return jsonify(success=False, message=f'抓取失败: {e}')


@app.route('/api/add_account', methods=['POST'])
@admin_api_required
@csrf_protected
def api_add_account():
    """添加要监控的公众号"""
    data = request.get_json(silent=True) or {}
    account = data.get('account', '').strip()

    if not account:
        return jsonify(success=False, message='公众号名称不能为空')
    if not wechat.is_authenticated():
        return jsonify(success=False, message='尚未登录微信，请先扫码登录')

    try:
        fakeid = wechat.get_fakeid(account)
        if not fakeid:
            return jsonify(success=False, message=f'找不到公众号：{account}，请检查名称是否正确')
        add_log(f'添加公众号：{account}（fakeid={fakeid}）')
        return jsonify(success=True, message=f'成功添加：{account}')

    except PermissionError as e:
        add_log(str(e), 'warning')
        return jsonify(success=False, message=str(e), need_login=True)
    except Exception as e:
        add_log(f'添加公众号失败: {e}', 'error')
        return jsonify(success=False, message=f'添加失败: {e}')


@app.route('/api/remove_account', methods=['POST'])
@admin_api_required
@csrf_protected
def api_remove_account():
    """从监控列表中移除公众号"""
    data = request.get_json(silent=True) or {}
    account = data.get('account', '').strip()

    if not account:
        return jsonify(success=False, message='公众号名称不能为空')

    cache = wechat._load_fakeid_cache()
    if account not in cache:
        return jsonify(success=False, message=f'列表中没有该公众号：{account}')

    del cache[account]
    wechat._save_fakeid_cache(cache)
    add_log(f'移除公众号：{account}')
    return jsonify(success=True, message=f'已移除：{account}')


@app.route('/api/publish', methods=['POST'])
@admin_api_required
@csrf_protected
def api_publish():
    """批量将待发布文章推送到 Discuz 论坛"""
    data = request.get_json(silent=True) or {}
    limit = data.get('limit', 20)

    pending = database.get_pending_articles(limit=limit)
    if not pending:
        return jsonify(success=True, message='没有待发布的文章', total=0, ok=0, fail=0)

    ok = fail = 0
    for art in pending:
        try:
            content = art.get('content') or f"来源公众号：{art.get('account_name')}"
            if art.get('article_url') and art['article_url'] not in content:
                content = f"{content}\n\n原文链接：{art['article_url']}"
            content = wechat.localize_images(content)
            database.publish_to_discuz(art['title'], content)
            database.mark_published(art['id'])
            ok += 1
        except Exception as e:
            fail += 1
            add_log(f'发布失败 [{art["title"][:20]}]: {e}', 'error')

    msg = f'发布完成：成功 {ok} 篇，失败 {fail} 篇'
    add_log(msg)
    return jsonify(success=True, message=msg, total=len(pending), ok=ok, fail=fail)


@app.route('/api/status')
@admin_api_required
def api_status():
    """返回系统状态 JSON"""
    try:
        stats = database.get_stats()
        auth_ok = wechat.is_authenticated()
        return jsonify(success=True, auth=auth_ok, **stats)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@app.route('/api/logs')
@admin_api_required
def api_logs():
    """返回最近操作日志"""
    return jsonify(logs=_op_logs[:100])


# ─── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    try:
        database.init_table()
        add_log('数据库表检查完成')
    except Exception as e:
        add_log(f'数据库初始化失败（请检查 .env 或 config.json 中的数据库配置）: {e}', 'error')

    host = cfg.WEB_HOST
    port = cfg.WEB_PORT
    print(f'\n启动成功，访问地址：http://{host if host != "0.0.0.0" else "127.0.0.1"}:{port}\n')
    app.run(host=host, port=port, debug=False, use_reloader=False)

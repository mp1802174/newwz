#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号管理系统 - Flask Web 应用
"""

import json
import logging
import datetime
import threading
from pathlib import Path

from flask import Flask, render_template, request, jsonify

import wechat
import database

# ─── 初始化 ────────────────────────────────────────────────────────────────────

cfg = json.loads((Path(__file__).parent / 'config.json').read_text(encoding='utf-8'))

app = Flask(__name__)
app.secret_key = cfg['web']['secret_key']

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


# ─── 页面路由 ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    try:
        stats = database.get_stats()
    except Exception as e:
        stats = {'total': '?', 'published': '?', 'pending': '?', 'accounts': '?'}
        add_log(f'获取统计信息失败: {e}', 'error')

    accounts = list(wechat._load_fakeid_cache().keys())
    auth_ok  = wechat.is_authenticated()
    auth_time = wechat.load_auth().get('saved_at', '未知')

    return render_template(
        'index.html',
        stats=stats,
        accounts=accounts,
        auth_ok=auth_ok,
        auth_time=auth_time,
        logs=_op_logs[:50],
    )


@app.route('/articles')
def articles():
    account = request.args.get('account') or None
    limit   = request.args.get('limit', 100, type=int)
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
    return render_template('articles.html', articles=rows, accounts=accounts,
                           current_account=account, limit=limit)


# ─── API 路由 ──────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    """在后台线程中触发微信扫码登录（浏览器会在服务器本机弹出）"""
    def _do_login():
        try:
            add_log('开始扫码登录流程...')
            wechat.login()
            add_log('登录成功')
        except Exception as e:
            add_log(f'登录失败: {e}', 'error')

    t = threading.Thread(target=_do_login, daemon=True)
    t.start()
    return jsonify(success=True, message='已在服务器后台启动登录流程，请在服务器本机浏览器中扫码')


@app.route('/api/crawl', methods=['POST'])
def api_crawl():
    """立即抓取所有公众号文章并入库"""
    limit = request.json.get('limit', 10) if request.is_json else 10

    if not wechat.is_authenticated():
        return jsonify(success=False, message='尚未登录微信，请先扫码登录')

    try:
        add_log(f'开始抓取，每个公众号最多 {limit} 篇...')
        articles = wechat.crawl_all(limit_per_account=limit)
        saved = database.save_articles(articles)
        msg = f'抓取完成，获取 {len(articles)} 篇，新入库 {saved} 篇'
        add_log(msg)
        return jsonify(success=True, message=msg, fetched=len(articles), saved=saved)

    except PermissionError as e:
        add_log(str(e), 'warning')
        return jsonify(success=False, message=str(e), need_login=True)
    except Exception as e:
        add_log(f'抓取失败: {e}', 'error')
        return jsonify(success=False, message=f'抓取失败: {e}')


@app.route('/api/crawl_account', methods=['POST'])
def api_crawl_account():
    """抓取指定单个公众号的文章"""
    data    = request.json or {}
    account = data.get('account', '').strip()
    limit   = data.get('limit', 10)

    if not account:
        return jsonify(success=False, message='请提供公众号名称')
    if not wechat.is_authenticated():
        return jsonify(success=False, message='尚未登录微信，请先扫码登录')

    try:
        add_log(f'抓取公众号：{account}')
        articles = wechat.get_articles(account, limit=limit)
        saved = database.save_articles(articles)
        msg = f'[{account}] 获取 {len(articles)} 篇，新入库 {saved} 篇'
        add_log(msg)
        return jsonify(success=True, message=msg, fetched=len(articles), saved=saved)

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
def api_add_account():
    """添加要监控的公众号"""
    data    = request.json or {}
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
def api_remove_account():
    """从监控列表中移除公众号"""
    data    = request.json or {}
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
def api_publish():
    """批量将待发布文章推送到 Discuz 论坛"""
    data  = request.json or {}
    limit = data.get('limit', 20)

    pending = database.get_pending_articles(limit=limit)
    if not pending:
        return jsonify(success=True, message='没有待发布的文章', total=0, ok=0, fail=0)

    ok = fail = 0
    for art in pending:
        try:
            content = art.get('content') or f"来源：{art.get('account_name')}"
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
def api_status():
    """返回系统状态 JSON"""
    try:
        stats   = database.get_stats()
        auth_ok = wechat.is_authenticated()
        return jsonify(success=True, auth=auth_ok, **stats)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@app.route('/api/logs')
def api_logs():
    """返回最近操作日志"""
    return jsonify(logs=_op_logs[:100])


# ─── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # 确保数据库表存在
    try:
        database.init_table()
        add_log('数据库表检查完成')
    except Exception as e:
        add_log(f'数据库初始化失败（请检查 config.json 中的数据库配置）: {e}', 'error')

    host = cfg['web']['host']
    port = cfg['web']['port']
    print(f'\n✅  启动成功，访问地址：http://{host if host != "0.0.0.0" else "127.0.0.1"}:{port}\n')
    app.run(host=host, port=port, debug=False, use_reloader=False)

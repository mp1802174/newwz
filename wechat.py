#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众平台 - 登录 & 文章抓取
"""

import re
import time
import json
import datetime
import requests
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://mp.weixin.qq.com/',
}


# ─── 凭据管理 ──────────────────────────────────────────────────────────────────

def load_auth():
    """加载登录凭据，返回 dict，无凭据时返回空 dict"""
    path = DATA_DIR / 'id_info.json'
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_auth(token, cookie):
    """保存登录凭据到 data/id_info.json"""
    DATA_DIR.mkdir(exist_ok=True)
    data = {
        'token': token,
        'cookie': cookie,
        'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    (DATA_DIR / 'id_info.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def is_authenticated():
    """判断当前是否有有效凭据"""
    auth = load_auth()
    return bool(auth.get('token') and auth.get('cookie'))


def _get_request_headers():
    """构造带 Cookie 的请求头"""
    auth = load_auth()
    h = HEADERS.copy()
    h['Cookie'] = auth.get('cookie', '')
    return h, auth.get('token', '')


# ─── 登录 ──────────────────────────────────────────────────────────────────────

def login(timeout=300):
    """
    使用 DrissionPage 打开浏览器，等待用户扫码登录微信公众平台。
    登录成功后自动提取 token + cookie 并保存。

    Args:
        timeout: 等待扫码的最长秒数，默认 300 秒

    Returns:
        (token, cookie)

    Raises:
        TimeoutError: 超时未扫码
        RuntimeError: 无法提取 token
    """
    from DrissionPage import ChromiumPage

    print('正在打开浏览器，请扫码登录微信公众平台...')
    bro = ChromiumPage()
    try:
        bro.get('https://mp.weixin.qq.com/')
        bro.set.window.max()

        start = time.time()
        while 'token=' not in bro.url:
            if time.time() - start > timeout:
                raise TimeoutError(f'登录超时（{timeout} 秒），请重试')
            time.sleep(1)

        match = re.search(r'token=(\w+)', bro.url)
        if not match:
            raise RuntimeError('登录后无法从 URL 提取 token')
        token = match.group(1)

        cookies = bro.cookies()
        cookie = '; '.join(f"{c['name']}={c['value']}" for c in cookies if c.get('name'))

        save_auth(token, cookie)
        print(f'登录成功！token={token}')
        return token, cookie

    finally:
        try:
            bro.quit()
        except Exception:
            pass


# ─── 公众号 fakeid 查询 ────────────────────────────────────────────────────────

def _load_fakeid_cache():
    path = DATA_DIR / 'name2fakeid.json'
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _save_fakeid_cache(cache):
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / 'name2fakeid.json').write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def get_fakeid(account_name):
    """
    通过公众号名称获取 fakeid（先查本地缓存，没有再调接口）。

    Returns:
        str fakeid，找不到返回 None

    Raises:
        PermissionError: 凭据过期
    """
    cache = _load_fakeid_cache()
    if account_name in cache:
        return cache[account_name]

    headers, token = _get_request_headers()
    resp = requests.get(
        'https://mp.weixin.qq.com/cgi-bin/searchbiz',
        params={
            'action': 'search_biz', 'begin': 0, 'count': 5,
            'query': account_name, 'token': token,
            'lang': 'zh_CN', 'f': 'json', 'ajax': 1,
        },
        headers=headers,
        timeout=15,
    ).json()

    _check_response(resp)

    for item in resp.get('list', []):
        if item.get('nickname') == account_name:
            fakeid = item['fakeid']
            cache[account_name] = fakeid
            _save_fakeid_cache(cache)
            return fakeid

    return None


# ─── 文章抓取 ──────────────────────────────────────────────────────────────────

def get_articles(account_name, limit=10):
    """
    获取指定公众号的最新文章列表。

    Args:
        account_name: 公众号名称
        limit: 最多返回的文章数量

    Returns:
        list of dict，每项包含 account_name / title / article_url / publish_timestamp

    Raises:
        PermissionError: 凭据过期，需重新登录
        RuntimeError: 请求频率过快
        ValueError: 找不到该公众号
    """
    fakeid = get_fakeid(account_name)
    if not fakeid:
        raise ValueError(f'找不到公众号：{account_name}')

    headers, token = _get_request_headers()
    resp = requests.get(
        'https://mp.weixin.qq.com/cgi-bin/appmsgpublish',
        params={
            'sub': 'list', 'search_field': 'null', 'begin': 0,
            'count': min(limit, 5), 'query': '', 'fakeid': fakeid,
            'type': '101_1', 'free_publish_type': 1, 'sub_action': 'list_ex',
            'token': token, 'lang': 'zh_CN', 'f': 'json', 'ajax': 1,
        },
        headers=headers,
        timeout=15,
    ).json()

    _check_response(resp)

    articles = []
    publish_page = resp.get('publish_page', '{}')
    for item in json.loads(publish_page).get('publish_list', []):
        publish_info = json.loads(item.get('publish_info', '{}'))
        for art in publish_info.get('appmsgex', []):
            create_time = art.get('create_time')
            if not create_time:
                continue
            # 微信时间戳：从 1970-01-01 08:00 开始的分钟数
            ts = datetime.datetime(1970, 1, 1, 8, 0) + datetime.timedelta(minutes=create_time // 60)
            articles.append({
                'account_name': account_name,
                'title':        art.get('title', ''),
                'article_url':  art.get('link', ''),
                'publish_timestamp': ts,
            })
            if len(articles) >= limit:
                return articles
    return articles


def crawl_all(limit_per_account=10):
    """
    抓取 name2fakeid.json 中所有公众号的文章。

    Returns:
        list of dict（同 get_articles）
    """
    accounts = list(_load_fakeid_cache().keys())
    if not accounts:
        print('没有配置公众号，请先在页面添加')
        return []

    all_articles = []
    for name in accounts:
        try:
            arts = get_articles(name, limit_per_account)
            all_articles.extend(arts)
            print(f'  [{name}] 获取 {len(arts)} 篇')
        except PermissionError:
            raise   # 凭据过期，向上抛出让调用方处理
        except Exception as e:
            print(f'  [{name}] 失败：{e}')
        time.sleep(1)   # 避免频率限制

    return all_articles


# ─── 内部工具 ──────────────────────────────────────────────────────────────────

def _check_response(resp):
    """检查微信 API 返回，异常时抛出对应错误"""
    if not isinstance(resp, dict):
        return
    err = resp.get('base_resp', {}).get('err_msg', '').lower()
    if any(k in err for k in ('invalid session', 'invalid csrf token', 'missing session')):
        raise PermissionError('微信凭据已过期，请重新扫码登录')
    if 'freq control' in err:
        raise RuntimeError('请求频率过快，请稍后再试')

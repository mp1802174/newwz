#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众平台 - 登录 & 文章抓取
"""

import base64
import datetime
import hashlib
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / 'data'
IMG_DIR = Path(__file__).parent / 'static' / 'imgs'
IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_SERVE_DIR = Path('/www/wwwroot/00077/wx-imgs')
IMG_SERVE_DIR.mkdir(parents=True, exist_ok=True)

# 图片服务基础 URL，从环境变量读取，默认本机
import os as _os
_IMG_BASE = _os.environ.get('IMG_BASE_URL', 'https://8wf.net/wx-imgs')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://mp.weixin.qq.com/',
}

_LOGIN_LOCK = threading.Lock()
_LOGIN_SESSION = {
    'status': 'idle',
    'message': '尚未启动扫码登录',
    'qr_base64': '',
    'started_at': '',
    'updated_at': '',
    'expires_in': 0,
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
    """判断当前是否有有效凭据（仅检查本地文件，不联网）"""
    auth = load_auth()
    return bool(auth.get('token') and auth.get('cookie'))


def verify_credentials():
    """
    向微信服务器发一次轻量请求，验证凭据是否仍有效。

    Returns:
        True  — 凭据有效
        False — 本地无凭据

    Raises:
        PermissionError: 凭据已过期（服务器返回 invalid session）
    """
    if not is_authenticated():
        return False
    headers, token = _get_request_headers()
    resp = requests.get(
        'https://mp.weixin.qq.com/cgi-bin/searchbiz',
        params={
            'action': 'search_biz', 'begin': 0, 'count': 1,
            'query': 'a', 'token': token,
            'lang': 'zh_CN', 'f': 'json', 'ajax': 1,
        },
        headers=headers,
        timeout=15,
    ).json()
    _check_response(resp)  # 凭据失效时抛出 PermissionError
    return True



def _get_request_headers():
    """构造带 Cookie 的请求头"""
    auth = load_auth()
    h = HEADERS.copy()
    h['Cookie'] = auth.get('cookie', '')
    return h, auth.get('token', '')


# ─── 登录状态管理 ──────────────────────────────────────────────────────────────


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')



def _set_login_session(**kwargs):
    with _LOGIN_LOCK:
        _LOGIN_SESSION.update(kwargs)
        _LOGIN_SESSION['updated_at'] = _now_str()
        return dict(_LOGIN_SESSION)



def get_login_session():
    with _LOGIN_LOCK:
        return dict(_LOGIN_SESSION)



def _extract_token(url):
    match = re.search(r'token=(\w+)', url or '')
    return match.group(1) if match else ''



def _extract_cookie_string(page):
    cookies = page.cookies()
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies if c.get('name'))



def _qr_base64_from_page(page):
    selectors = [
        'css:.login__type__container img',
        'css:.qrcode_login_container img',
        'css:.login__main__qrcode img',
        'css:img[alt*="二维码"]',
        'tag:img',
    ]
    for selector in selectors:
        try:
            ele = page.ele(selector, timeout=3)
            if ele:
                b64 = ele.get_screenshot(as_base64='png')
                if b64:
                    return b64
        except Exception:
            continue

    try:
        b64 = page.get_screenshot(as_base64='png')
        if b64:
            return b64
    except Exception:
        pass

    return ''



def _run_web_login(timeout):
    browser = None
    try:
        from DrissionPage import Chromium, ChromiumOptions

        co = ChromiumOptions()
        co.headless().set_argument('--window-size', '1280,900').set_argument('--no-sandbox')
        browser = Chromium(co)
        page = browser.latest_tab
        page.get('https://mp.weixin.qq.com/')
        time.sleep(2)

        qr_base64 = _qr_base64_from_page(page)
        if not qr_base64:
            raise RuntimeError('未能获取登录二维码截图')

        _set_login_session(
            status='pending',
            message='二维码已生成，请在网页中扫码登录微信公众平台',
            qr_base64=f'data:image/png;base64,{qr_base64}',
            started_at=_now_str(),
            expires_in=timeout,
        )

        started = time.time()
        last_url = page.url
        while True:
            current_url = page.url
            if current_url != last_url and current_url:
                last_url = current_url
                token = _extract_token(current_url)
                if token:
                    cookie = _extract_cookie_string(page)
                    save_auth(token, cookie)
                    _set_login_session(
                        status='success',
                        message='微信登录成功，凭据已保存',
                        qr_base64='',
                        expires_in=0,
                    )
                    return

            elapsed = time.time() - started
            if elapsed > timeout:
                _set_login_session(
                    status='timeout',
                    message=f'登录超时（{timeout} 秒），请重新生成二维码',
                    qr_base64='',
                    expires_in=0,
                )
                return

            if int(elapsed) % 5 == 0:
                _set_login_session(expires_in=max(0, timeout - int(elapsed)))
            time.sleep(1)

    except Exception as e:
        _set_login_session(
            status='error',
            message=f'网页登录失败：{e}',
            qr_base64='',
            expires_in=0,
        )
    finally:
        if browser:
            try:
                browser.quit()
            except Exception:
                pass



def start_web_login(timeout=300):
    """启动网页可见的扫码登录流程。"""
    with _LOGIN_LOCK:
        status = _LOGIN_SESSION.get('status')
        if status in {'starting', 'pending'}:
            return dict(_LOGIN_SESSION)
        _LOGIN_SESSION.update({
            'status': 'starting',
            'message': '正在生成二维码，请稍候...',
            'qr_base64': '',
            'started_at': _now_str(),
            'updated_at': _now_str(),
            'expires_in': timeout,
        })

    thread = threading.Thread(target=_run_web_login, args=(timeout,), daemon=True)
    thread.start()
    return get_login_session()



def login(timeout=300):
    """兼容旧调用：启动网页扫码登录并等待成功。"""
    start_web_login(timeout=timeout)
    started = time.time()
    while time.time() - started <= timeout:
        session = get_login_session()
        if session['status'] == 'success':
            auth = load_auth()
            return auth.get('token'), auth.get('cookie')
        if session['status'] in {'error', 'timeout'}:
            raise RuntimeError(session['message'])
        time.sleep(1)
    raise TimeoutError(f'登录超时（{timeout} 秒），请重试')


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


def _download_image(url):
    """下载微信图片到本地，返回本机访问 URL；失败则返回原 URL。"""
    try:
        ext = 'jpg'
        parsed = urllib.parse.urlparse(url)
        fmt = urllib.parse.parse_qs(parsed.query).get('wx_fmt', [''])[0]
        if fmt in ('png', 'gif', 'webp', 'jpeg'):
            ext = 'jpg' if fmt == 'jpeg' else fmt
        name = hashlib.md5(url.encode()).hexdigest() + '.' + ext
        dest = IMG_DIR / name
        if not dest.exists():
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
        # 同步复制到网站静态目录
        serve_dest = IMG_SERVE_DIR / name
        if not serve_dest.exists():
            serve_dest.write_bytes(dest.read_bytes())
        return f'{_IMG_BASE}/{name}'
    except Exception:
        return url


def localize_images(content):
    """将正文中所有微信图片链接替换为本地链接。"""
    import re as _re
    def _replace(m):
        url = m.group(1)
        if 'mmbiz.qpic.cn' in url or 'mmbiz.qlogo.cn' in url:
            return f'[img]{_download_image(url)}[/img]'
        return m.group(0)
    return _re.sub(r'\[img\](https?://[^\[]+)\[/img\]', _replace, content)


def fetch_article_content(article_url):
    """抓取文章正文并转换为 BBCode 格式（保留图片）。"""
    resp = requests.get(article_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, 'html.parser')

    title = ''
    title_ele = soup.select_one('#activity-name')
    if title_ele:
        title = title_ele.get_text(' ', strip=True)

    content_root = soup.select_one('#js_content') or soup.select_one('.rich_media_content')
    if not content_root:
        raise RuntimeError('未找到文章正文区域')

    # 将 data-src 赋值给 src，确保图片链接可用
    for img in content_root.find_all('img'):
        src = img.get('data-src') or img.get('src', '')
        if src:
            img['src'] = src

    parts = []
    for node in content_root.descendants:
        if node.name == 'img':
            src = node.get('src', '')
            if src and src.startswith('http'):
                local_url = _download_image(src)
                parts.append(f'[img]{local_url}[/img]')
        elif node.name is None:  # 文本节点
            text = node.strip()
            if text:
                # 避免重复追加（父节点已处理过的子文本）
                parent = node.parent
                if parent and parent.name in ('p', 'section', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'strong', 'em', 'li'):
                    parts.append(text)

    body = '\n'.join(parts)
    if not body.strip():
        raise RuntimeError('文章正文为空')

    prefix = f'{title}\n\n' if title else ''
    return f'{prefix}{body}\n\n原文链接：{article_url}'



def get_articles(account_name, limit=10):
    """
    获取指定公众号的最新文章列表。

    Args:
        account_name: 公众号名称
        limit: 最多返回的文章数量

    Returns:
        list of dict，每项包含 account_name / title / article_url / publish_timestamp / content

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
            ts = datetime.datetime(1970, 1, 1, 8, 0) + datetime.timedelta(minutes=create_time // 60)
            article_url = art.get('link', '')
            content = None
            if article_url:
                try:
                    content = fetch_article_content(article_url)
                except Exception:
                    content = None
            articles.append({
                'account_name': account_name,
                'title': art.get('title', ''),
                'article_url': article_url,
                'publish_timestamp': ts,
                'content': content,
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
            raise
        except Exception as e:
            print(f'  [{name}] 失败：{e}')
        time.sleep(1)

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

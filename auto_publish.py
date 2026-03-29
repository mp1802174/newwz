#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时任务：每天凌晨 2 点自动抓取各公众号最新文章并发布到论坛。
- 每个公众号只抓取当天尚未入库的新文章，不补抓旧文章
- 全部公众号合并后按发布时间倒序取最多 5 篇发布
- 不够 5 篇不做任何补充
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import database
import wechat

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / 'auto_publish.log', encoding='utf-8'),
    ],
)
log = logging.getLogger(__name__)

PUBLISH_LIMIT = 5   # 每天最多发布篇数
CRAWL_PER_ACCOUNT = 5  # 每个公众号抓取条数（只取最新，已存在的会被去重）


def run():
    if not wechat.is_authenticated():
        log.error('微信凭据未登录或已失效，请手动扫码登录后再运行')
        sys.exit(1)

    try:
        wechat.verify_credentials()
        log.info('微信凭据验证通过')
    except PermissionError as e:
        log.error(f'微信凭据已过期，请重新扫码登录: {e}')
        sys.exit(1)
    except Exception as e:
        log.warning(f'凭据联网验证失败（网络问题？），继续执行: {e}')

    accounts = list(wechat._load_fakeid_cache().keys())
    if not accounts:
        log.warning('没有配置任何公众号')
        return

    log.info(f'开始抓取，共 {len(accounts)} 个公众号，每个最多 {CRAWL_PER_ACCOUNT} 篇')
    all_articles = []
    for acc in accounts:
        name = acc
        try:
            arts = wechat.get_articles(name, limit=CRAWL_PER_ACCOUNT)
            all_articles.extend(arts)
            log.info(f'[{name}] 获取 {len(arts)} 篇')
        except PermissionError as e:
            log.error(f'凭据失效: {e}')
            sys.exit(1)
        except Exception as e:
            log.warning(f'[{name}] 抓取失败: {e}')

    if not all_articles:
        log.info('没有获取到任何文章')
        return

    result = database.save_articles(all_articles)
    log.info(f'入库完成：新增 {result["inserted"]} 篇，补全 {result["updated"]} 篇')

    # 取待发布文章（按发布时间倒序，最多 PUBLISH_LIMIT 篇）
    pending = database.get_pending_articles(limit=PUBLISH_LIMIT)
    if not pending:
        log.info('没有待发布的新文章')
        return

    log.info(f'准备发布 {len(pending)} 篇')
    ok = fail = 0
    for art in pending:
        try:
            content = art.get('content') or f"来源公众号：{art.get('account_name')}"
            if art.get('article_url') and art['article_url'] not in content:
                content = f"{content}\n\n原文链接：{art['article_url']}"
            content = wechat.localize_images(content)
            database.publish_to_discuz(art['title'], content)
            database.mark_published(art['id'])
            log.info(f'已发布：{art["title"][:40]}')
            ok += 1
        except Exception as e:
            log.error(f'发布失败 [{art["title"][:20]}]: {e}')
            fail += 1

    log.info(f'发布完成：成功 {ok} 篇，失败 {fail} 篇')


if __name__ == '__main__':
    run()

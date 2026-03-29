#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库操作 - WZ主库 & Discuz论坛库
"""

import json
import time
import datetime
from pathlib import Path

import mysql.connector


# ─── 配置加载 ──────────────────────────────────────────────────────────────────

def _load_config():
    path = Path(__file__).parent / 'config.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _wz_conn():
    """获取 WZ 主库连接"""
    cfg = _load_config()['wz_db']
    return mysql.connector.connect(**cfg, autocommit=True)


def _discuz_conn():
    """获取 Discuz 论坛库连接"""
    cfg = _load_config()['discuz_db']
    return mysql.connector.connect(**cfg, autocommit=False)


# ─── 初始化 ────────────────────────────────────────────────────────────────────

def init_table():
    """
    确保 wechat_articles 表存在，不存在则创建。
    首次部署时调用一次即可。
    """
    conn = _wz_conn()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS wechat_articles (
            id                INT AUTO_INCREMENT PRIMARY KEY,
            account_name      VARCHAR(255)  NOT NULL COMMENT '公众号名称',
            title             VARCHAR(512)  NOT NULL COMMENT '文章标题',
            article_url       VARCHAR(1024) NOT NULL COMMENT '文章链接',
            publish_timestamp DATETIME      NOT NULL COMMENT '发布时间',
            content           LONGTEXT               COMMENT '文章正文（可选）',
            source_type       VARCHAR(50)   DEFAULT 'wechat' COMMENT '来源类型',
            forum_published   TINYINT(1)             COMMENT '是否已发布到论坛',
            fetched_at        TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_url (article_url(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cursor.close()
    conn.close()


# ─── 文章入库 ──────────────────────────────────────────────────────────────────

def save_articles(articles):
    """
    批量保存文章，URL 已存在则跳过（INSERT IGNORE）。

    Args:
        articles: list of dict，每项需含：
                  account_name / title / article_url / publish_timestamp

    Returns:
        int: 新增入库的行数
    """
    if not articles:
        return 0

    conn = _wz_conn()
    cursor = conn.cursor()

    sql = """
        INSERT IGNORE INTO wechat_articles
            (account_name, title, article_url, publish_timestamp, source_type)
        VALUES (%s, %s, %s, %s, 'wechat')
    """
    rows = [
        (
            a['account_name'],
            a['title'],
            a['article_url'],
            a['publish_timestamp'] if isinstance(a['publish_timestamp'], datetime.datetime)
            else datetime.datetime.now(),
        )
        for a in articles
    ]

    cursor.executemany(sql, rows)
    count = cursor.rowcount
    cursor.close()
    conn.close()
    return count


# ─── 文章查询 ──────────────────────────────────────────────────────────────────

def get_articles(limit=100, account=None):
    """
    查询已入库的文章。

    Args:
        limit:   返回条数上限
        account: 指定公众号名称，None 则返回全部

    Returns:
        list of dict
    """
    conn = _wz_conn()
    cursor = conn.cursor(dictionary=True)

    if account:
        cursor.execute(
            "SELECT * FROM wechat_articles WHERE account_name = %s "
            "ORDER BY publish_timestamp DESC LIMIT %s",
            (account, limit),
        )
    else:
        cursor.execute(
            "SELECT * FROM wechat_articles ORDER BY publish_timestamp DESC LIMIT %s",
            (limit,),
        )

    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_stats():
    """
    返回主库的简要统计信息。

    Returns:
        dict: total / published / pending / accounts
    """
    conn = _wz_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM wechat_articles")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM wechat_articles WHERE forum_published = 1")
    published = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM wechat_articles "
        "WHERE forum_published IS NULL AND content IS NOT NULL AND LENGTH(content) > 50"
    )
    pending = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT account_name) FROM wechat_articles")
    accounts = cursor.fetchone()[0]

    cursor.close()
    conn.close()
    return {
        'total': total,
        'published': published,
        'pending': pending,
        'accounts': accounts,
    }


# ─── 论坛发布 ──────────────────────────────────────────────────────────────────

def get_pending_articles(limit=100):
    """
    获取待发布到论坛的文章（已有正文、尚未发布）。

    Returns:
        list of dict: id / title / content / account_name / publish_timestamp
    """
    conn = _wz_conn()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT id, title, content, account_name, publish_timestamp
        FROM wechat_articles
        WHERE forum_published IS NULL
          AND content IS NOT NULL
          AND LENGTH(content) > 50
        ORDER BY publish_timestamp DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def mark_published(article_id):
    """将文章标记为已发布到论坛"""
    conn = _wz_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE wechat_articles SET forum_published = 1 WHERE id = %s",
        (article_id,),
    )
    conn.commit()
    cursor.close()
    conn.close()


def publish_to_discuz(title, content):
    """
    直接写入 Discuz 数据库完成发帖。

    Args:
        title:   帖子标题
        content: 帖子正文

    Returns:
        int: 新帖子的 tid

    Raises:
        Exception: 数据库写入失败时抛出
    """
    cfg = _load_config()['forum']
    fid      = cfg['fid']
    author   = cfg['author']
    authorid = cfg['authorid']
    now      = int(time.time())

    conn = _discuz_conn()
    cursor = conn.cursor()

    try:
        # 获取下一个可用 tid / pid
        cursor.execute("SELECT MAX(tid) FROM pre_forum_thread")
        tid = (cursor.fetchone()[0] or 0) + 1

        cursor.execute("SELECT MAX(pid) FROM pre_forum_post")
        pid = (cursor.fetchone()[0] or 0) + 1

        # 插入主题
        cursor.execute(
            """
            INSERT INTO pre_forum_thread
                (tid, fid, author, authorid, subject, dateline, lastpost, lastposter,
                 views, replies, displayorder, digest, special, attachment, moderated,
                 closed, stickreply, recommends, recommend_add, recommend_sub, heats,
                 status, isgroup, favtimes, sharetimes, stamp, icon, pushedaid, cover,
                 replycredit, relatebytag, maxposition, bgcolor, comments, hidden)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s,
                 0, 0, 0, 0, 0, 0, 0,
                 0, 0, 0, 0, 0, 0,
                 0, 0, 0, 0, -1, -1, 0, 0,
                 0, '', 1, '', 0, 0)
            """,
            (tid, fid, author, authorid, title, now, now, author),
        )

        # 插入帖子正文
        cursor.execute(
            """
            INSERT INTO pre_forum_post
                (pid, fid, tid, repid, first, author, authorid, subject, dateline,
                 lastupdate, updateuid, premsg, message, useip, port, invisible,
                 anonymous, usesig, htmlon, bbcodeoff, smileyoff, parseurloff,
                 attachment, rate, ratetimes, status, tags, comment, replycredit, position)
            VALUES
                (%s, %s, %s, 0, 1, %s, %s, %s, %s,
                 0, 0, '', %s, '', 0, 0,
                 0, 1, 0, 0, 0, 0,
                 0, 0, 0, 0, '', 0, 0, 1)
            """,
            (pid, fid, tid, author, authorid, title, now, content),
        )

        # 更新版块统计
        cursor.execute(
            "UPDATE pre_forum_forum SET threads = threads + 1, posts = posts + 1 WHERE fid = %s",
            (fid,),
        )

        # 更新用户统计
        cursor.execute(
            "UPDATE pre_common_member_count SET posts = posts + 1, threads = threads + 1 WHERE uid = %s",
            (authorid,),
        )

        conn.commit()
        return tid

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()

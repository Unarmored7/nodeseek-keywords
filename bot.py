"""
Telegram bot: command handlers and the recurring RSS-poll job.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config
import monitor
import storage

logger = logging.getLogger(__name__)

_MAX_KEYWORD_LENGTH = 128
_MAX_REGEX_LENGTH = 128

# ── Module-level state ─────────────────────────────────────────────────────────

# Tracks consecutive RSS fetch failures for health alerting
_rss_fail_count: int = 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _esc(text: object) -> str:
    """Escape text for safe inclusion in HTML parse-mode messages."""
    return html.escape(str(text))


def _safe_link(link: str, label: Optional[str] = None) -> str:
    """Render a safe external link for Telegram HTML messages."""
    text = _esc(label or link)
    parts = urlsplit(link)
    if parts.scheme == "https" and parts.netloc:
        return f'<a href="{_esc(link)}">{text}</a>'
    return text


def _authorized(update: Update) -> bool:
    return update.effective_user.id == config.ALLOWED_USER_ID


async def _send_with_retry(
    bot,
    chat_id: int,
    text: str,
    max_retries: int = 3,
    **kwargs,
) -> bool:
    """
    Send a Telegram message with exponential-backoff retry.
    Returns True on success, False after all retries are exhausted.
    """
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return True
        except Exception as exc:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, …
            else:
                logger.error(
                    "Failed to send message after %d retries: %s", max_retries, exc
                )
    return False


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "👋 <b>NodeSeek 关键词监控 Bot</b>\n\n"
        "<b>命令列表：</b>\n"
        "/add <code>&lt;关键词&gt;</code> <i>[--regex] [分类]</i>  — 添加监控关键词\n"
        "/remove <code>&lt;关键词&gt;</code>  — 删除关键词（含所有分类）\n"
        "/pause <code>&lt;关键词&gt;</code>  — 暂停关键词（不删除）\n"
        "/resume <code>&lt;关键词&gt;</code>  — 恢复已暂停的关键词\n"
        "/list  — 查看所有监控关键词\n"
        "/history <i>[数量]</i>  — 查看最近推送记录（默认 10 条）\n"
        "/categories  — 查看可用版块分类\n"
        "/status  — 查看 Bot 运行状态\n\n"
        "💡 <i>不填分类则监控全部版块；可多次 /add 同一关键词搭配不同分类。</i>\n"
        "🔍 <i>加 --regex 启用正则匹配，例：/add DMIT.*(CN2|GIA) --regex trade</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "用法：/add <code>&lt;关键词&gt;</code> <i>[--regex] [版块]</i>\n\n"
            "<b>普通模式（子串匹配）：</b>\n"
            "  /add DMIT\n"
            "  /add 搬瓦工 trade\n\n"
            "<b>正则模式（--regex，不区分大小写）：</b>\n"
            "  /add DMIT.*(CN2|GIA) --regex\n"
            "    <i>含 CN2 或 GIA 的 DMIT 帖</i>\n"
            "  /add 套餐.*\\d+[Gg] --regex trade\n"
            "    <i>交易版中带容量数字的套餐帖</i>\n"
            "  /add (补货|回归|上新) --regex info\n"
            "    <i>情报版的补货/回归/上新帖</i>\n"
            "  /add ^\\[.*(促销|限时).* --regex\n"
            "    <i>标题开头带促销或限时标签的帖</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    parts = list(context.args)

    # Extract --regex flag
    match_mode = "substring"
    if "--regex" in parts:
        match_mode = "regex"
        parts.remove("--regex")

    # Extract category (last token if it's a known category slug)
    category: Optional[str] = None
    if parts and parts[-1].lower() in monitor.CATEGORIES:
        category = parts.pop().lower()

    keyword = " ".join(parts)

    if not keyword:
        await update.message.reply_text("❌ 关键词不能为空。")
        return

    if len(keyword) > _MAX_KEYWORD_LENGTH:
        await update.message.reply_text(
            f"❌ 关键词过长，最多 {_MAX_KEYWORD_LENGTH} 个字符。"
        )
        return

    # Validate regex syntax upfront to give immediate feedback
    if match_mode == "regex":
        if len(keyword) > _MAX_REGEX_LENGTH:
            await update.message.reply_text(
                f"❌ 正则表达式过长，最多 {_MAX_REGEX_LENGTH} 个字符。"
            )
            return
        try:
            re.compile(keyword)
        except re.error as exc:
            await update.message.reply_text(
                f"❌ 正则表达式无效：<code>{_esc(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    ok = storage.add_keyword(keyword, category, match_mode)
    if ok:
        cat_str = (
            f"，仅限 <b>{_esc(monitor.CATEGORIES[category])}</b> 版块"
            if category
            else "，监控全部版块"
        )
        mode_str = " 🔍 <i>正则模式</i>" if match_mode == "regex" else ""
        await update.message.reply_text(
            f"✅ 已添加关键词 <code>{_esc(keyword)}</code>{cat_str}{mode_str}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"⚠️ 关键词 <code>{_esc(keyword)}</code>"
            + (f" ({_esc(category)})" if category else "")
            + " 已存在，无需重复添加。",
            parse_mode=ParseMode.HTML,
        )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "用法：/remove <code>&lt;关键词&gt;</code>\n"
            "将删除该关键词下所有分类的记录。",
            parse_mode=ParseMode.HTML,
        )
        return

    keyword = " ".join(context.args)
    count = storage.remove_keyword(keyword)
    if count:
        await update.message.reply_text(
            f"✅ 已删除关键词 <code>{_esc(keyword)}</code>（共 {count} 条记录）",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ 未找到关键词 <code>{_esc(keyword)}</code>，请用 /list 确认拼写。",
            parse_mode=ParseMode.HTML,
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    keywords = storage.list_keywords()
    if not keywords:
        await update.message.reply_text(
            "📋 暂无监控关键词。\n使用 /add 添加第一个。"
        )
        return

    lines = [f"📋 <b>监控关键词（共 {len(keywords)} 条）：</b>\n"]
    for i, kw in enumerate(keywords, 1):
        scope = (
            f"<i>{_esc(monitor.CATEGORIES.get(kw['category'], kw['category']))}</i>"
            if kw["category"]
            else "<i>全部版块</i>"
        )
        mode_tag   = " 🔍" if kw["match_mode"] == "regex" else ""
        status_tag = " ⏸" if not kw["enabled"] else ""
        lines.append(
            f"{i}. <code>{_esc(kw['keyword'])}</code>{mode_tag}{status_tag} — {scope}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "用法：/pause <code>&lt;关键词&gt;</code>\n"
            "暂停监控但不删除，可用 /resume 恢复。",
            parse_mode=ParseMode.HTML,
        )
        return

    keyword = " ".join(context.args)
    count = storage.set_keyword_enabled(keyword, False)
    if count:
        await update.message.reply_text(
            f"⏸ 已暂停关键词 <code>{_esc(keyword)}</code>（{count} 条记录）",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ 未找到关键词 <code>{_esc(keyword)}</code>，请用 /list 确认拼写。",
            parse_mode=ParseMode.HTML,
        )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "用法：/resume <code>&lt;关键词&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    keyword = " ".join(context.args)
    count = storage.set_keyword_enabled(keyword, True)
    if count:
        await update.message.reply_text(
            f"▶️ 已恢复关键词 <code>{_esc(keyword)}</code>（{count} 条记录）",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ 未找到关键词 <code>{_esc(keyword)}</code>，请用 /list 确认拼写。",
            parse_mode=ParseMode.HTML,
        )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(20, int(context.args[0])))
        except ValueError:
            pass

    records = storage.get_history(limit)
    if not records:
        await update.message.reply_text("📭 暂无推送记录。")
        return

    parts = [f"📜 <b>最近 {len(records)} 条推送记录：</b>"]
    for r in records:
        cat_name  = monitor.CATEGORIES.get(r["category"], r["category"])
        sent_at   = r["sent_at"][:16].replace("T", " ")
        kw_tags   = " ".join(
            f"<code>{_esc(k.strip())}</code>" for k in r["keywords"].split(",")
        )
        status_icon = "❌ " if r["status"] == "failed" else ""
        parts.append(
            f"{status_icon}{kw_tags} · <i>{_esc(cat_name)}</i> · {sent_at}\n"
            f"  {_safe_link(r['link'], r['title'])}"
        )

    await update.message.reply_text(
        "\n\n".join(parts),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    lines = ["🏷 <b>可用版块分类：</b>\n"]
    for slug, name in monitor.CATEGORIES.items():
        lines.append(f"• <code>{slug}</code> — {name}")
    lines.append("\n示例：/add DMIT trade")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    all_keywords = storage.list_keywords()
    active  = sum(1 for kw in all_keywords if kw["enabled"])
    paused  = len(all_keywords) - active
    initialized = storage.get_setting("initialized") == "true"

    paused_line = f"  ⏸ 已暂停：{paused} 个\n" if paused else ""
    await update.message.reply_text(
        f"✅ <b>Bot 运行正常</b>\n\n"
        f"📊 监控关键词：{active} 个\n"
        f"{paused_line}"
        f"⏱ 轮询间隔：{config.POLL_INTERVAL} 秒\n"
        f"🚦 防洪上限：{config.MAX_NOTIFICATIONS_PER_POLL} 条/轮\n"
        f"🌐 RSS 地址：<code>{config.RSS_BASE_URL}</code>\n"
        f"🔄 已初始化：{'是' if initialized else '否（首次轮询后完成）'}",
        parse_mode=ParseMode.HTML,
    )


# ── Notification formatter ─────────────────────────────────────────────────────

def _build_notification(post: dict, matched_keywords: list[str]) -> str:
    kw_tags  = " ".join(f"<code>{_esc(k)}</code>" for k in matched_keywords)
    cat_name = monitor.CATEGORIES.get(post["category"], post["category"])
    return (
        f"🔔 <b>关键词提醒</b>  {kw_tags}\n\n"
        f"📌 <b>{_esc(post['title'])}</b>\n"
        f"🏷 {_esc(cat_name)}\n"
        f"👤 {_esc(post['author'])}\n"
        f"🔗 {_safe_link(post['link'])}"
    )


# ── RSS polling job (runs on bot's event loop via JobQueue) ───────────────────

async def poll_rss(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Called every POLL_INTERVAL seconds by PTB's JobQueue.

    Strategy:
    - First run  : seed all current posts as "seen" — no notifications sent.
    - Normal runs: for each new unseen post, check enabled keyword matches.

    Reliability  : each send is retried up to 3× with exponential backoff;
                   failed sends are logged in the notifications table with
                   status='failed' so /history can surface them.

    Flood guard  : at most MAX_NOTIFICATIONS_PER_POLL individual messages are
                   sent per cycle; any overflow is collapsed into one summary.

    RSS health   : consecutive fetch failures increment a counter; once it
                   reaches RSS_FAIL_ALERT_THRESHOLD the user is notified.
    """
    global _rss_fail_count

    # Only consider enabled keywords
    keywords = [kw for kw in storage.list_keywords() if kw["enabled"]]
    if not keywords:
        return

    # Determine which category feeds to request
    need_global   = any(kw["category"] is None for kw in keywords)
    specific_cats: set[str] = {
        kw["category"] for kw in keywords if kw["category"] is not None
    }

    # ── Fetch RSS ──────────────────────────────────────────────────────────────
    entries: dict[int, dict] = {}
    try:
        if need_global:
            for e in await monitor.fetch_entries():
                entries[e["post_id"]] = e
        else:
            for cat in specific_cats:
                for e in await monitor.fetch_entries(cat):
                    entries[e["post_id"]] = e
    except Exception as exc:
        logger.error("RSS fetch failed: %s", exc)
        _rss_fail_count += 1
        if _rss_fail_count == config.RSS_FAIL_ALERT_THRESHOLD:
            try:
                await context.bot.send_message(
                    chat_id=config.ALLOWED_USER_ID,
                    text=(
                        f"⚠️ <b>RSS 拉取连续失败 {_rss_fail_count} 次</b>\n\n"
                        f"数据源：<code>{_esc(config.RSS_BASE_URL)}</code>\n"
                        "请检查网络连接或数据源是否正常。"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    _rss_fail_count = 0  # Reset on successful fetch

    if not entries:
        return

    # ── First-run: seed without notifying ─────────────────────────────────────
    if storage.get_setting("initialized") != "true":
        logger.info("First poll — seeding %d posts as seen (no notifications)", len(entries))
        storage.mark_many_seen(list(entries.keys()))
        storage.set_setting("initialized", "true")
        return

    # ── Normal run: collect matches ────────────────────────────────────────────
    notifications: list[tuple[dict, list[str]]] = []  # [(post, matched_keywords)]
    new_post_count = 0

    for post_id, post in sorted(entries.items()):
        if storage.is_seen(post_id):
            continue
        storage.mark_seen(post_id)
        new_post_count += 1

        matched = [
            kw["keyword"]
            for kw in keywords
            if (kw["category"] is None or kw["category"] == post["category"])
            and monitor.matches(post["title"], kw["keyword"], kw["match_mode"])
        ]
        if matched:
            notifications.append((post, matched))

    if new_post_count:
        logger.debug(
            "Poll — %d new post(s), %d with keyword match(es)",
            new_post_count, len(notifications),
        )

    if not notifications:
        return

    logger.info("Poll — sending %d notification(s)", len(notifications))

    # ── Flood guard: individual sends up to cap; overflow → one summary ────────
    cap      = config.MAX_NOTIFICATIONS_PER_POLL
    to_send  = notifications[:cap]
    overflow = notifications[cap:]

    sent_count = 0
    for post, matched_kws in to_send:
        msg     = _build_notification(post, matched_kws)
        success = await _send_with_retry(
            context.bot,
            config.ALLOWED_USER_ID,
            msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        status = "sent" if success else "failed"
        for kw in matched_kws:
            storage.log_notification(
                post["post_id"], kw, post["title"],
                post["link"], post["category"], post["author"], status,
            )
        if success:
            sent_count += 1
        await asyncio.sleep(0.3)  # Stay within Telegram rate limits

    # Send overflow summary
    if overflow:
        summary = [
            f"⚠️ <b>本轮匹配 {len(notifications)} 条，已单独推送 {len(to_send)} 条。"
            f"以下 {len(overflow)} 条已自动汇总：</b>\n"
        ]
        for post, matched_kws in overflow:
            kw_str = " ".join(f"<code>{_esc(k)}</code>" for k in matched_kws)
            summary.append(
                f"• {kw_str} — "
                f"{_safe_link(post['link'], post['title'])}"
            )
        try:
            await context.bot.send_message(
                chat_id=config.ALLOWED_USER_ID,
                text="\n".join(summary),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            for post, matched_kws in overflow:
                for kw in matched_kws:
                    storage.log_notification(
                        post["post_id"], kw, post["title"],
                        post["link"], post["category"], post["author"], "sent",
                    )
        except Exception as exc:
            logger.error("Failed to send overflow summary: %s", exc)
            for post, matched_kws in overflow:
                for kw in matched_kws:
                    storage.log_notification(
                        post["post_id"], kw, post["title"],
                        post["link"], post["category"], post["author"], "failed",
                    )

    logger.info(
        "Poll complete — %d sent, %d in overflow summary", sent_count, len(overflow)
    )

    # Periodic DB cleanup
    storage.cleanup_old_seen(keep_days=7)
    storage.cleanup_old_notifications(keep_days=30)

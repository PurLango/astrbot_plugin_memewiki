from __future__ import annotations

import asyncio
import contextlib
import datetime
import html
import json
import re
import shutil
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request


PAGE_API_PREFIX = "/astrbot_plugin_memewiki/page"
PUBLIC_API = "https://cnmeme.wiki/api/v1/public/memes"
SOURCE_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SOURCE_TIMEZONE)
    return parsed.astimezone(datetime.timezone.utc)


def _newest_cursor(existing: str, candidate: str) -> str:
    if not candidate:
        return existing
    if not existing:
        return candidate
    existing_time = _parse_timestamp(existing)
    candidate_time = _parse_timestamp(candidate)
    if candidate_time is not None and (
        existing_time is None or candidate_time > existing_time
    ):
        return candidate
    return existing


def _inline_text(value: Any) -> str:
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\u00a0", " ")
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _block_text(value: Any) -> str:
    blocks: list[str] = []
    pattern = re.compile(
        r"<(p|ul|ol|blockquote|pre)[^>]*>([\s\S]*?)</\1>", re.I
    )
    for match in pattern.finditer(str(value or "")):
        tag, body = match.group(1).lower(), match.group(2)
        if tag in {"ul", "ol"}:
            items = [
                re.sub(r"\s*\n\s*", " ", _inline_text(item.group(1))).strip()
                for item in re.finditer(r"<li[^>]*>([\s\S]*?)</li>", body, re.I)
            ]
            items = [item for item in items if item]
            if items:
                blocks.append("\n\n".join(f"- {item}" for item in items))
        else:
            text = _inline_text(body)
            if text:
                blocks.append(text)
    return "\n\n".join(blocks)


def _split_sections(value: Any) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"<h2[^>]*>([\s\S]*?)</h2>([\s\S]*?)(?=<h2|$)", re.I
    )
    return [
        (_inline_text(match.group(1)), _block_text(match.group(2)))
        for match in pattern.finditer(str(value or ""))
        if _inline_text(match.group(1))
    ]


def _api_item_to_row(item: dict[str, Any]) -> dict[str, str]:
    sections = _split_sections(item.get("content_html"))
    section_map = {title: content for title, content in sections}
    others = "\n\n".join(
        f"{title}\n{content}" if content else title
        for title, content in sections
        if title not in {"来源", "用法"}
    )

    def text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    tags = item.get("tags")
    aliases = item.get("aliases")
    return {
        "title": text(item.get("title")),
        "slug": text(item.get("slug")) or text(item.get("title")),
        "tags": "、".join(map(text, tags)) if isinstance(tags, list) else text(tags),
        "usage": section_map.get("用法", "").strip(),
        "origin": section_map.get("来源", "").strip(),
        "other": others.strip(),
        "summary": text(item.get("summary")),
        "aliases": ",".join(map(text, aliases)) if isinstance(aliases, list) else text(aliases),
    }


class MemeWikiPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._registered_routes: list[tuple[str, Any]] = []
        self._sync_task: asyncio.Task | None = None
        self._status_lock = threading.Lock()
        self._sync_status: dict[str, Any] = {
            "state": "idle",
            "phase": "",
            "started_at": "",
            "finished_at": "",
            "scanned": 0,
            "pending": 0,
            "fetched": 0,
            "added": 0,
            "error": "",
            "logs": [],
        }

    def register_routes(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return
        routes = (
            ("/state", self.state, ["GET"], "MemeWiki state"),
            ("/entries", self.entries, ["GET", "POST"], "MemeWiki entries"),
            ("/entry", self.entry, ["GET"], "MemeWiki entry"),
            ("/entry/save", self.save_entry, ["POST"], "Save MemeWiki entry"),
            ("/entry/delete", self.delete_entry, ["POST"], "Delete MemeWiki entry"),
            ("/sync/start", self.start_sync, ["POST"], "Start MemeWiki sync"),
        )
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        snapshot = list(registered) if isinstance(registered, list) else None
        try:
            for path, handler, methods, description in routes:
                route = f"{PAGE_API_PREFIX}{path}"
                register(route, handler, methods, description)
                self._registered_routes.append((route, handler))
        except Exception:
            if snapshot is not None:
                registered[:] = snapshot
            self._registered_routes.clear()
            raise

    def unregister_routes(self) -> None:
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        if not isinstance(registered, list):
            return
        owned = {(route, id(handler)) for route, handler in self._registered_routes}
        registered[:] = [
            item
            for item in registered
            if not (
                isinstance(item, tuple)
                and len(item) >= 2
                and (item[0], id(item[1])) in owned
            )
        ]
        self._registered_routes.clear()

    async def stop_sync(self) -> None:
        task = self._sync_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @staticmethod
    async def _payload() -> dict[str, Any]:
        value = await request.json(default={})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _ok(data: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "data": data}

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"status": "error", "message": message, "data": {}}

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    def _status_snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            return json.loads(json.dumps(self._sync_status, ensure_ascii=False))

    def _set_status(self, **changes: Any) -> None:
        with self._status_lock:
            self._sync_status.update(changes)

    def _log_status(self, message: str) -> None:
        with self._status_lock:
            logs = self._sync_status.setdefault("logs", [])
            logs.append(str(message))
            del logs[:-30]

    def _database_state_sync(self) -> dict[str, Any]:
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=3)
        ) as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0])
            meta = {}
            with contextlib.suppress(sqlite3.Error):
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return {
            "total": total,
            "built_at": str(meta.get("built_at", "")),
            "source": str(meta.get("source", "cnmeme.wiki")),
            "database_size": self.plugin._db_path.stat().st_size,
        }

    async def state(self) -> dict[str, Any]:
        try:
            data = await asyncio.to_thread(self._database_state_sync)
            data["sync"] = self._status_snapshot()
            return self._ok(data)
        except Exception as exc:
            logger.warning("[memewiki] 读取管理页状态失败：%s", exc)
            return self._error("读取词库状态失败")

    def _list_entries_sync(
        self, query: str, page: int, page_size: int
    ) -> dict[str, Any]:
        where = ""
        params: list[Any] = []
        if query:
            where = (
                "WHERE instr(lower(title), lower(?)) > 0 "
                "OR instr(lower(COALESCE(aliases, '')), lower(?)) > 0 "
                "OR instr(lower(COALESCE(tags, '')), lower(?)) > 0"
            )
            params.extend([query, query, query])
        offset = (page - 1) * page_size
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=3)
        ) as conn:
            conn.row_factory = sqlite3.Row
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM memes {where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT title, slug, tags, summary, aliases FROM memes {where} "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    async def entries(self) -> dict[str, Any]:
        try:
            payload = await self._payload()
            values = payload or request.args
            query = str(values.get("query") or "").strip()[:100]
            page = self._bounded_int(values.get("page"), 1, 1, 100000)
            page_size = self._bounded_int(
                values.get("page_size"), 30, 10, 50
            )
            data = await asyncio.to_thread(
                self._list_entries_sync, query, page, page_size
            )
            return self._ok(data)
        except Exception as exc:
            logger.warning("[memewiki] 搜索管理页词条失败：%s", exc)
            return self._error("搜索词条失败")

    def _entry_sync(self, title: str) -> dict[str, Any] | None:
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=3)
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, slug, tags, usage, origin, other, summary, aliases "
                "FROM memes WHERE title = ?",
                (title,),
            ).fetchone()
        return dict(row) if row is not None else None

    async def entry(self) -> dict[str, Any]:
        title = str(request.args.get("title") or "").strip()[:200]
        if not title:
            return self._error("缺少词条名称")
        data = await asyncio.to_thread(self._entry_sync, title)
        return self._ok({"entry": data}) if data else self._error("词条不存在")

    @staticmethod
    def _validated_entry(payload: dict[str, Any]) -> tuple[dict[str, str], str]:
        limits = {
            "title": 100,
            "slug": 200,
            "tags": 500,
            "usage": 10000,
            "origin": 10000,
            "other": 10000,
            "summary": 4000,
            "aliases": 1000,
        }
        result = {
            key: str(payload.get(key) or "").strip()[:limit]
            for key, limit in limits.items()
        }
        if not result["title"]:
            return {}, "词条名称不能为空"
        if not result["slug"]:
            result["slug"] = result["title"]
        return result, ""

    def _save_entry_sync(self, original_title: str, entry: dict[str, str]) -> bool:
        fields = ("slug", "tags", "usage", "origin", "other", "summary", "aliases")
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=8)
        ) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if original_title:
                exists = conn.execute(
                    "SELECT 1 FROM memes WHERE title = ?", (original_title,)
                ).fetchone()
                if exists is None:
                    raise ValueError("原词条已不存在，请刷新后重试")
                if entry["title"] != original_title:
                    conflict = conn.execute(
                        "SELECT 1 FROM memes WHERE title = ?", (entry["title"],)
                    ).fetchone()
                    if conflict is not None:
                        raise ValueError("新名称已经被其他词条使用")
                conn.execute(
                    "UPDATE memes SET title=?, slug=?, tags=?, usage=?, origin=?, other=?, "
                    "summary=?, aliases=? WHERE title=?",
                    (entry["title"], *(entry[field] for field in fields), original_title),
                )
                created = False
            else:
                conflict = conn.execute(
                    "SELECT 1 FROM memes WHERE title = ?", (entry["title"],)
                ).fetchone()
                if conflict is not None:
                    raise ValueError("词条名称已经存在")
                conn.execute(
                    "INSERT INTO memes (title, slug, tags, usage, origin, other, summary, aliases) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry["title"], *(entry[field] for field in fields)),
                )
                created = True
            conn.commit()
        return created

    async def save_entry(self) -> dict[str, Any]:
        payload = await self._payload()
        entry, error = self._validated_entry(payload.get("entry", {}))
        if error:
            return self._error(error)
        original_title = str(payload.get("original_title") or "").strip()[:100]
        try:
            async with self.plugin._db_write_lock:
                created = await asyncio.to_thread(
                    self._save_entry_sync, original_title, entry
                )
                await asyncio.to_thread(self.plugin._load_database_index)
            return self._ok({"entry": entry, "created": created})
        except ValueError as exc:
            return self._error(str(exc))
        except Exception as exc:
            logger.warning("[memewiki] 保存词条失败：%s", exc)
            return self._error("保存词条失败")

    def _delete_entry_sync(self, title: str) -> bool:
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=8)
        ) as conn:
            cursor = conn.execute("DELETE FROM memes WHERE title = ?", (title,))
            conn.commit()
            return cursor.rowcount > 0

    async def delete_entry(self) -> dict[str, Any]:
        payload = await self._payload()
        title = str(payload.get("title") or "").strip()[:100]
        if not title:
            return self._error("缺少词条名称")
        try:
            async with self.plugin._db_write_lock:
                deleted = await asyncio.to_thread(self._delete_entry_sync, title)
                if deleted:
                    await asyncio.to_thread(self.plugin._load_database_index)
            return self._ok({"deleted": deleted}) if deleted else self._error("词条不存在")
        except Exception as exc:
            logger.warning("[memewiki] 删除词条失败：%s", exc)
            return self._error("删除词条失败")

    @staticmethod
    def _fetch_json_sync(url: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "astrbot-plugin-memewiki/0.2"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _fetch_json(self, url: str, retries: int = 4) -> dict[str, Any]:
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(self._fetch_json_sync, url)
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                if attempt == retries - 1:
                    raise RuntimeError(f"请求失败：{exc}") from exc
                await asyncio.sleep(min(8, 1.5 * (2**attempt)))
        raise RuntimeError("请求重试次数已用尽")

    def _local_identity_sync(self) -> tuple[set[str], set[str], str, str]:
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=3)
        ) as conn:
            rows = conn.execute("SELECT title, slug FROM memes").fetchall()
            meta = {}
            with contextlib.suppress(sqlite3.Error):
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return (
            {str(row[0]) for row in rows},
            {str(row[1] or "") for row in rows},
            str(meta.get("last_remote_updated_at", "")),
            str(meta.get("built_at", "")),
        )

    def _apply_sync_result_sync(
        self, rows: list[dict[str, str]], remote_cursor: str
    ) -> int:
        if rows:
            backup_dir = self.plugin._data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.plugin._db_path, backup_dir / f"memes-{stamp}.db")
        inserted = 0
        with contextlib.closing(
            sqlite3.connect(str(self.plugin._db_path), timeout=8)
        ) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                if not row["title"]:
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO memes "
                    "(title, slug, tags, usage, origin, other, summary, aliases) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["title"],
                        row["slug"],
                        row["tags"],
                        row["usage"],
                        row["origin"],
                        row["other"],
                        row["summary"],
                        row["aliases"],
                    ),
                )
                inserted += max(0, cursor.rowcount)
            total = int(conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0])
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            if inserted:
                built_at = datetime.datetime.now().isoformat(
                    sep=" ", timespec="seconds"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)",
                    (built_at,),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('count', ?)",
                (str(total),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('source', ?)",
                ("cnmeme.wiki /api/v1/public/memes",),
            )
            if remote_cursor:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('last_remote_updated_at', ?)",
                    (remote_cursor,),
                )
            conn.commit()
        return inserted

    async def _run_sync(self) -> None:
        try:
            self._set_status(state="running", phase="检查最新词条", scanned=0)
            self._log_status("开始检查 cnmeme.wiki 最新词条")
            local_titles, local_slugs, remote_cursor, built_at = (
                await asyncio.to_thread(self._local_identity_sync)
            )
            cutoff_text = remote_cursor or built_at
            cutoff = _parse_timestamp(cutoff_text)
            if not remote_cursor and cutoff is not None:
                cutoff -= datetime.timedelta(days=1)
            if remote_cursor:
                self._log_status("已读取上次更新位置，仅检查之后的内容")
            elif cutoff is not None:
                self._log_status("首次增量更新，按本地构建时间回看一天")
            site_items: list[dict[str, Any]] = []
            offset = 0
            newest_cursor = ""
            reached_cutoff = False
            while True:
                data = await self._fetch_json(f"{PUBLIC_API}?limit=30&offset={offset}")
                items = data.get("items", []) if isinstance(data, dict) else []
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    updated_text = str(item.get("updated_at") or "").strip()
                    updated_at = _parse_timestamp(updated_text)
                    if not newest_cursor and updated_text:
                        newest_cursor = updated_text
                    if cutoff is not None and updated_at is not None and updated_at < cutoff:
                        reached_cutoff = True
                        break
                    site_items.append(item)
                offset += len(items)
                self._set_status(scanned=len(site_items))
                if offset % 300 == 0:
                    self._log_status(f"已检查 {len(site_items)} 条最新词条")
                if reached_cutoff or not data.get("has_more"):
                    break
                await asyncio.sleep(0.12)

            pending = [
                item
                for item in site_items
                if str(item.get("title") or "") not in local_titles
                and str(item.get("slug") or "") not in local_slugs
            ]
            self._set_status(phase="下载新增词条", pending=len(pending), fetched=0)
            self._log_status(
                f"已检查 {len(site_items)} 条最新内容，发现 {len(pending)} 条新增"
            )
            cursor_to_save = _newest_cursor(remote_cursor, newest_cursor)
            if not pending:
                async with self.plugin._db_write_lock:
                    await asyncio.to_thread(
                        self._apply_sync_result_sync, [], cursor_to_save
                    )
                self._set_status(
                    state="done",
                    phase="已是最新",
                    finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
                )
                self._log_status("词库已经是最新版本")
                return

            rows: list[dict[str, str]] = []
            for index, item in enumerate(pending, 1):
                slug = urllib.parse.quote(str(item.get("slug") or ""), safe="")
                if not slug:
                    continue
                detail = await self._fetch_json(f"{PUBLIC_API}/{slug}")
                body = detail.get("item") if isinstance(detail, dict) else None
                if not isinstance(body, dict):
                    body = detail
                if isinstance(body, dict):
                    rows.append(_api_item_to_row(body))
                self._set_status(fetched=index)
                if index % 10 == 0 or index == len(pending):
                    self._log_status(f"已下载 {index}/{len(pending)} 条详情")
                await asyncio.sleep(0.25)

            self._set_status(phase="写入本地词库")
            async with self.plugin._db_write_lock:
                added = await asyncio.to_thread(
                    self._apply_sync_result_sync, rows, cursor_to_save
                )
                await asyncio.to_thread(self.plugin._load_database_index)
            self._set_status(
                state="done",
                phase="更新完成",
                added=added,
                finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )
            self._log_status(f"更新完成，新增 {added} 条")
        except asyncio.CancelledError:
            self._set_status(state="idle", phase="同步已停止")
            raise
        except Exception as exc:
            logger.warning("[memewiki] 词库同步失败：%s", exc)
            self._set_status(
                state="error",
                phase="更新失败",
                error=str(exc),
                finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )
            self._log_status(f"更新失败：{exc}")

    async def start_sync(self) -> dict[str, Any]:
        if self._sync_task is not None and not self._sync_task.done():
            return self._error("词库正在更新，请等待完成")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._status_lock:
            self._sync_status = {
                "state": "running",
                "phase": "准备更新",
                "started_at": now,
                "finished_at": "",
                "scanned": 0,
                "pending": 0,
                "fetched": 0,
                "added": 0,
                "error": "",
                "logs": [],
            }
        self._sync_task = asyncio.create_task(self._run_sync())
        return self._ok({"sync": self._status_snapshot()})

"""法规变更监控：定时抓官方发布，识别新文件。

技术方案 8.2 的要求是"RSS/公告页抓取 + 人工录入通道（双保险）"。
本实现用国家税务总局政策法规库的公开列表接口轮询，
与库里已有的（按来源 URL 与标题双重比对）做差集，得到"新文件"。

两个设计要点：
  1. **按来源 URL 判重，不看标题**。标题会被重新排版（空格、书名号差异），
     按标题判重会反复把同一份文件当成新发现。
  2. **只发现、不自动入库**。发现是机器的事，入库要过复核——
     自动把抓到的文件直接发布，等于绕过了整套审核与门禁。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evolution import EvolutionEvent
from app.models.knowledge import Regulation

LIST_API = "https://www.chinatax.gov.cn/getFileListByCodeId"
DETAIL_HOST = "https://fgk.chinatax.gov.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 监控哪些栏目。与采集脚本保持一致，避免"采集看这几个栏目、监控看那几个"的错位。
CHANNELS: dict[str, str] = {
    "法律": "d34fa7ad03f84f4caed12f5c2beae099",
    "行政法规": "e1cd1569d1ea4a25a11041248925a081",
    "国务院文件": "fa1726b47078490fa0a4522194185e8d",
    "税务规范性文件": "470b437b304f434396500a1e2edc7f28",
    "财税文件": "2cb303fdee614232b79552d52bb057d6",
}


@dataclass
class DiscoveredDocument:
    """一条新发现的文件。"""

    title: str
    url: str
    published: str = ""
    channel: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "published": self.published,
            "channel": self.channel,
        }


@dataclass
class MonitorScan:
    """一次扫描的结果。"""

    discovered: list[DiscoveredDocument] = field(default_factory=list)
    scanned: int = 0
    channels: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "discovered": [item.to_dict() for item in self.discovered],
            "count": len(self.discovered),
            "scanned": self.scanned,
            "channels": dict(self.channels),
            "errors": list(self.errors),
        }


class OfficialMonitor:
    """轮询官方栏目，找出库里还没有的文件。"""

    def __init__(self, session: Session, *, pages: int = 2, size: int = 20, sleep: float = 0.3):
        self.session = session
        self.pages = pages
        self.size = size
        self.sleep = sleep
        self.known_urls = self._load_known_urls()

    def _load_known_urls(self) -> set[str]:
        urls = self.session.execute(select(Regulation.source_url)).scalars().all()
        return {self._normalize_url(url) for url in urls if url}

    @staticmethod
    def _normalize_url(url: str) -> str:
        return (url or "").strip().replace("http://www.chinatax.gov.cn", DETAIL_HOST)

    # ------------------------------------------------------------------
    def scan(self, *, channels: list[str] | None = None) -> MonitorScan:
        result = MonitorScan()
        for name in channels or list(CHANNELS):
            channel_id = CHANNELS.get(name)
            if not channel_id:
                result.errors.append(f"未知栏目：{name}")
                continue
            found = 0
            for page in range(1, self.pages + 1):
                try:
                    data = self._fetch_page(channel_id, page)
                except Exception as exc:  # noqa: BLE001 - 单栏目失败不影响其它栏目
                    result.errors.append(f"{name} 第 {page} 页取列表失败：{exc}")
                    break
                items = (data.get("results") or []) if isinstance(data, dict) else []
                if not items:
                    break
                for item in items:
                    result.scanned += 1
                    title = (item.get("title") or "").strip()
                    url = self._normalize_url(item.get("url") or "")
                    if not title or not url or url in self.known_urls:
                        continue
                    result.discovered.append(
                        DiscoveredDocument(
                            title=title,
                            url=url,
                            published=(item.get("publishedTimeStr") or "")[:10],
                            channel=name,
                        )
                    )
                    self.known_urls.add(url)
                    found += 1
                time.sleep(self.sleep)
            result.channels[name] = found
        return result

    def record(self, document: DiscoveredDocument, *, event_type: str = "new_regulation") -> EvolutionEvent:
        """把发现记入变更事件表（G8 的计时起点）。"""

        event = EvolutionEvent(
            event_type=event_type,
            status="discovered",
            title=document.title,
            source_url=document.url,
            discovered_at=datetime.now(timezone.utc),
        )
        self.session.add(event)
        self.session.commit()
        return event

    # ------------------------------------------------------------------
    @staticmethod
    def _fetch_page(channel_id: str, page: int, size: int = 20, retries: int = 2) -> dict:
        body = urllib.parse.urlencode(
            {"codeId": "", "channelId": channel_id, "page": page, "size": size}
        ).encode("utf-8")
        last: Exception | None = None
        for attempt in range(retries):
            try:
                request = urllib.request.Request(
                    LIST_API,
                    data=body,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                return (payload.get("results") or {}).get("data") or {}
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"取列表失败：{last}")

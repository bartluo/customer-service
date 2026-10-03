"""政策变化提醒：生成变化摘要。

技术方案 8.2 把"政策变化提醒"称为**独立卖点**：
"企业最怕的就是不知道政策变了"。

本模块负责**生成摘要**；推送通道（邮件 / 短信 / 站内信）留作接口——
当前环境没有配置任何推送渠道，硬写一个假的"已推送"比不写更糟：
用户会以为订阅生效了。

摘要本身按"对用户有没有实质影响"排序：废止/修订 > 新增场景 > 解读文章。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evolution import EvolutionEvent

# 事件类型 → 重要性与说明
IMPORTANCE = {
    "repealed": (1, "有法规被废止，原有做法可能已不适用"),
    "amended": (2, "有法规被修订，请注意条款变化"),
    "new_regulation": (3, "有新的政策文件发布"),
    "interpretation": (4, "有新的政策解读，供参考"),
}


@dataclass
class ChangeDigest:
    """一份政策变化摘要。"""

    generated_at: str
    items: list[dict] = field(default_factory=list)
    # 推送通道未配置时明确说明，不假装已发送
    delivery: str = "未配置推送通道（摘要已生成，可供人工转发或接入站内信）"

    @property
    def count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "count": self.count,
            "delivery": self.delivery,
            "items": list(self.items),
        }

    def text(self) -> str:
        lines = [f"政策变化摘要（{self.generated_at}）：共 {self.count} 条"]
        for item in self.items:
            lines.append(f"  {item['importance']}. {item['title']}")
            if item.get("document_number"):
                lines.append(f"     {item['document_number']}")
            lines.append(f"     {item['note']}")
            if item.get("affected"):
                lines.append(f"     受影响：{'、'.join(item['affected'][:3])}")
        lines.append(f"推送状态：{self.delivery}")
        return "\n".join(lines)


def build_change_digest(
    session: Session, *, since: datetime | None = None, limit: int = 50
) -> ChangeDigest:
    """把变更事件整理成一份可读摘要。"""

    statement = select(EvolutionEvent).order_by(EvolutionEvent.created_at.desc()).limit(limit)
    if since is not None:
        statement = (
            select(EvolutionEvent)
            .where(EvolutionEvent.created_at >= since)
            .order_by(EvolutionEvent.created_at.desc())
            .limit(limit)
        )
    events = session.execute(statement).scalars().all()

    items: list[dict] = []
    for event in events:
        rank, note = IMPORTANCE.get(event.event_type, (9, "其他变化"))
        impacted = (event.impact or {}).get("impacted") or []
        items.append(
            {
                "importance": rank,
                "title": event.title,
                "document_number": event.document_number,
                "event_type": event.event_type,
                "status": event.status,
                "note": note,
                "source_url": event.source_url,
                "affected": [entry.get("title", "") for entry in impacted],
                "discovered_at": event.discovered_at.isoformat() if event.discovered_at else None,
            }
        )
    items.sort(key=lambda item: item["importance"])
    return ChangeDigest(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items,
    )

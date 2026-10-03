"""答案模板引擎（技术方案 8）。

三条硬约束：
  1. `required: true` 的段落缺失 → 视为生成失败（不是"少一段也没关系"）。
  2. 免责段由模板渲染固定文案，**不依赖模型**，所以它 100% 出现。
  3. 输出结构化 JSON：前端按字段渲染，不靠解析自然语言。

为什么"缺必填段就失败"而不是补个空段：
  空段在界面上看起来像"这段没内容"，用户不会意识到系统其实出错了。
  宁可明确返回失败并降级（例如改走追问模板），也不要给一个看起来正常、
  实际少了一半内容的答案。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from app.config import domain_file

_DEFAULT_TEMPLATE_FILE = domain_file("finance_tax", "templates.yaml")


class TemplateError(ValueError):
    """模板本身有问题（缺字段、必填段没定义等）。"""


@dataclass(frozen=True)
class TemplateSection:
    key: str
    title: str
    required: bool
    renderer: str
    text: str = ""  # fixed_text 用的固定文案
    require_fields: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class AnswerTemplate:
    template_id: str
    name: str
    sections: tuple[TemplateSection, ...]
    description: str = ""

    def required_keys(self) -> tuple[str, ...]:
        return tuple(section.key for section in self.sections if section.required)

    def section(self, key: str) -> TemplateSection | None:
        for item in self.sections:
            if item.key == key:
                return item
        return None


def load_templates(
    path: str | pathlib.Path | None = None,
) -> dict[str, AnswerTemplate]:
    """读模板文件。返回 {template_id: 模板}。"""

    file_path = pathlib.Path(path) if path else _DEFAULT_TEMPLATE_FILE
    if not file_path.exists():
        raise TemplateError(f"模板文件不存在：{file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list) or not raw_templates:
        raise TemplateError("模板文件里没有 templates")

    templates: dict[str, AnswerTemplate] = {}
    for raw in raw_templates:
        template_id = raw.get("template_id")
        if not template_id:
            raise TemplateError(f"模板缺少 template_id：{raw!r}")
        sections = []
        for item in raw.get("sections") or []:
            for required_key in ("key", "title", "renderer"):
                if not item.get(required_key):
                    raise TemplateError(f"模板 {template_id} 的段落缺少 {required_key}：{item!r}")
            if item["renderer"] == "fixed_text" and not item.get("text"):
                raise TemplateError(
                    f"模板 {template_id} 的固定文案段落 {item['key']} 没有 text"
                )
            sections.append(
                TemplateSection(
                    key=item["key"],
                    title=item["title"],
                    required=bool(item.get("required")),
                    renderer=item["renderer"],
                    text=item.get("text", ""),
                    require_fields=tuple(item.get("require_fields") or ()),
                    description=item.get("description", ""),
                )
            )
        if not any(section.required for section in sections):
            raise TemplateError(f"模板 {template_id} 一个必填段都没有，等于没有约束力")
        templates[template_id] = AnswerTemplate(
            template_id=template_id,
            name=raw.get("name", template_id),
            sections=tuple(sections),
            description=raw.get("description", ""),
        )
    return templates


@dataclass
class RenderedSection:
    key: str
    title: str
    renderer: str
    content: object

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "renderer": self.renderer,
            "content": self.content,
        }


@dataclass
class RenderedAnswer:
    """渲染结果。ok=False 表示生成失败（必填段缺失），调用方要降级。"""

    template_id: str
    sections: list[RenderedSection] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    failure_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "ok": self.ok,
            "failure_reason": self.failure_reason,
            "missing_required": list(self.missing_required),
            "sections": [section.to_dict() for section in self.sections],
        }

    def to_text(self) -> str:
        lines: list[str] = []
        for section in self.sections:
            lines.append(f"【{section.title}】")
            content = section.content
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        lines.append("  · " + _render_item(section.renderer, item))
                    else:
                        lines.append(f"  · {item}")
            elif content:
                lines.append(f"  {content}")
            lines.append("")
        return "\n".join(lines).strip()


def _render_item(renderer: str, item: dict) -> str:
    """按渲染器把一段结构化内容转成人看的文字。

    结构化输出是给前端用的，但命令行/日志也要能读——
    直接打印字典会变成 `{'title': ...}` 这种没法看的东西。
    """

    if renderer == "formula_steps":
        head = item.get("title", "")
        substitution = item.get("substitution", "")
        result = item.get("result", "")
        citation = item.get("citation", "")
        text = f"{head}：{item.get('formula', '')}"
        if substitution:
            text += f"　代入 {substitution}"
        if result:
            text += f" = {result}"
        if citation:
            text += f"（依据：{citation}）"
        return text
    if renderer == "citation_list":
        text = item.get("text", "")
        reason = item.get("reason", "")
        return f"{text}" + (f"　—— {reason}" if reason else "")
    if renderer == "ordered_steps":
        parts = [item.get("title", "")]
        if item.get("channel"):
            parts.append(f"渠道：{item['channel']}")
        if item.get("materials"):
            parts.append(f"材料：{item['materials']}")
        return "；".join(part for part in parts if part)
    return item.get("text", str(item))


def render(template: AnswerTemplate, contents: dict) -> RenderedAnswer:
    """按模板渲染。

    contents 里没有的段落：必填 → 记入 missing_required（生成失败）；
    选填 → 直接不渲染那一段。
    fixed_text 段落永远由模板自己填，忽略调用方传的内容——
    这是免责声明 100% 出现的保证。
    """

    answer = RenderedAnswer(template_id=template.template_id)
    for section in template.sections:
        if section.renderer == "fixed_text":
            answer.sections.append(
                RenderedSection(section.key, section.title, section.renderer, section.text)
            )
            continue

        if section.key not in contents:
            if section.required:
                answer.missing_required.append(section.key)
            continue

        content = contents[section.key]
        if content in (None, "", [], {}):
            if section.required:
                answer.missing_required.append(section.key)
            continue

        if section.require_fields and isinstance(content, list):
            # 段落级字段要求：办理步骤必须带渠道与材料，否则不完整
            for item in content:
                if isinstance(item, dict) and any(
                    field_name not in item for field_name in section.require_fields
                ):
                    if section.required:
                        answer.missing_required.append(section.key)
                    break

        answer.sections.append(
            RenderedSection(section.key, section.title, section.renderer, content)
        )

    if answer.missing_required:
        answer.failure_reason = (
            "缺少必填段落：" + "、".join(answer.missing_required) + "（按规格判为生成失败）"
        )
    return answer

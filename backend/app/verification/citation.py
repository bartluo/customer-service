"""引用验证器。

检查引用的文号与条款号是否**真实存在**。

为什么这条要一票否决：引用错误是"编造依据"，比算错更严重——
它伪造了权威性。用户看到"财税〔2020〕999号 第三条规定……"，
不会怀疑这份文件根本不存在。

三层核对：
  1. 文号在库里存在吗
  2. 条款号在这份法规里存在吗
  3. 引用的条文内容与库里存的一致吗（防止"张冠李戴"）
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.verification.result import Issue, Severity, VerificationInput, VerifierResult


class CitationVerifier:
    """核对答案里的每一条引用。"""

    name = "引用验证器"

    def __init__(self, session: Session) -> None:
        self.session = session

    def verify(self, payload: VerificationInput) -> VerifierResult:
        result = VerifierResult(name=self.name)
        for citation in payload.citations:
            result.checked += 1
            result.issues.extend(self._check_one(citation))
        return result

    # ------------------------------------------------------------------
    def _check_one(self, citation: dict) -> list[Issue]:
        issues: list[Issue] = []
        document_number = (citation.get("document_number") or "").strip()
        full_no = (citation.get("full_no") or "").strip()
        title = (citation.get("regulation_title") or "").strip()

        regulation = self._find_regulation(document_number, title)
        if regulation is None:
            label = document_number or title or "(未标注文号)"
            issues.append(
                Issue(
                    verifier="citation",
                    severity=Severity.BLOCKING,
                    code="citation_document_not_found",
                    message=f"引用无效：知识库里不存在“{label}”这份法规",
                    target=label,
                )
            )
            return issues

        if full_no and not self._article_exists(regulation.id, full_no):
            issues.append(
                Issue(
                    verifier="citation",
                    severity=Severity.BLOCKING,
                    code="citation_article_not_found",
                    message=(
                        f"引用无效：“{regulation.title}”里没有“{full_no}”这一条"
                    ),
                    target=f"{regulation.title} {full_no}",
                )
            )

        # 内容一致性：库里这条条款的正文是否包含答案里那段话的关键部分
        issues.extend(self._check_content(regulation.id, full_no, citation))
        return issues

    def _find_regulation(self, document_number: str, title: str) -> Regulation | None:
        """按文号找；没有文号时才按标题找。

        关键：**给了文号就只按文号找，找不到就是引用无效**。
        如果文号找不到还退回按标题匹配，等于把"编造文号"这个错误放过去了——
        标题恰好对得上，伪造的文号就蒙混过关，而这正是引用验证要拦的头号问题。
        """

        if document_number:
            return self.session.execute(
                select(Regulation).where(Regulation.document_number == document_number)
            ).scalars().first()
        if title:
            return self.session.execute(
                select(Regulation).where(Regulation.title == title)
            ).scalars().first()
        return None

    def _article_exists(self, regulation_id: str, full_no: str) -> bool:
        """条款号核对。

        两个都要处理：
          · 库里的完整编号带空格（"第十条 （一）"），要先归一化；
          · 库里的完整编号**带章节前缀**（"第四章 税收优惠 第二十三条"），
            而引用通常只写"第二十三条"。

        所以用"包含"而不是"相等"。这不会把"第十条"误判成"第二十条"——
        "第十条"在"第二十条"里不是连续子串。
        """

        normalized = re.sub(r"\s+", "", full_no)
        rows = self.session.execute(
            select(RegulationArticle.full_no).where(
                RegulationArticle.regulation_id == regulation_id
            )
        ).scalars().all()
        return any(normalized in re.sub(r"\s+", "", item or "") for item in rows)

    def _check_content(self, regulation_id: str, full_no: str, citation: dict) -> list[Issue]:
        """内容一致性：答案里引的那段话，是不是真的来自这一条。

        只做"严重不符"的拦截（引文与库内条文毫无重叠），
        不做字面严格比对——答案里的引文通常会做省略和改写。
        """

        cited_content = (citation.get("content") or "").strip()
        if not cited_content or not full_no:
            return []
        rows = self.session.execute(
            select(RegulationArticleVersion.content)
            .join(RegulationArticle, RegulationArticle.id == RegulationArticleVersion.article_id)
            .where(
                RegulationArticle.regulation_id == regulation_id,
                RegulationArticle.full_no == full_no,
            )
        ).scalars().all()
        if not rows:
            return []
        overlap = max(self._overlap(cited_content, stored) for stored in rows)
        if overlap < 0.2:
            return [
                Issue(
                    verifier="citation",
                    severity=Severity.BLOCKING,
                    code="citation_content_mismatch",
                    message=f"引用内容与库内条文不符：“{full_no}”",
                    target=f"{full_no}: {cited_content[:40]}…",
                )
            ]
        return []

    @staticmethod
    def _overlap(left: str, right: str) -> float:
        """粗粒度重合度：按 3 字滑窗算共现比例。"""

        def grams(text: str) -> set[str]:
            clean = re.sub(r"\s+", "", text)
            return {clean[i : i + 3] for i in range(max(len(clean) - 2, 0))}

        left_grams = grams(left)
        if not left_grams:
            return 1.0
        return len(left_grams & grams(right)) / len(left_grams)

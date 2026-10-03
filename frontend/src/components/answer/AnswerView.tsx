"use client";

import type { AnswerBody, AnswerSection, CalculationStep, Citation } from "@/lib/types";
import { CitationCard } from "./CitationCard";

// 六段式答案渲染。
//
// 渲染方式由段落自带的 renderer 决定，前端不猜格式：
// 后端改了模板（例如给"办理步骤"加字段），这里跟着 renderer 走就行，
// 不需要前端同步改代码。这是"结构化输出而非纯文本"的全部意义。
export function AnswerView({
  answer,
  citations,
}: {
  answer: AnswerBody;
  citations: Citation[];
}) {
  return (
    <div className="space-y-5">
      {answer.sections.map((section) => (
        <Section key={section.key} section={section} citations={citations} />
      ))}
      {!answer.ok ? (
        <p className="text-[13px] text-[var(--danger)]">
          答案生成不完整（缺少必填段：{answer.missing_required.join("、")}）：{answer.failure_reason}
        </p>
      ) : null}
    </div>
  );
}

function Section({ section, citations }: { section: AnswerSection; citations: Citation[] }) {
  return (
    <section aria-labelledby={`sec-${section.key}`}>
      <h2 id={`sec-${section.key}`} className="mb-1.5 text-[14px] font-semibold">
        {section.title}
      </h2>
      <SectionBody section={section} citations={citations} />
    </section>
  );
}

function SectionBody({ section, citations }: { section: AnswerSection; citations: Citation[] }) {
  const content = section.content;

  if (section.renderer === "fixed_text") {
    // 免责声明用弱化样式：它必须一直在，但不该抢走正文的注意力
    return <p className="subtle text-[12.5px] leading-6">{String(content ?? "")}</p>;
  }

  if (section.renderer === "citation_list") {
    const items = Array.isArray(content) ? (content as Array<Record<string, string>>) : [];
    if (!items.length) return <p className="subtle text-[13px]">本次回答未引用条款。</p>;
    return (
      <ul className="space-y-2">
        {items.map((item, index) => {
          const matched = matchCitation(item.text ?? item.title ?? "", citations);
          return (
            <li key={`${item.text ?? index}-${index}`}>
              {matched ? (
                <CitationCard citation={matched} />
              ) : (
                <div className="rounded border px-3 py-2 text-[13px]" style={{ borderColor: "var(--border)" }}>
                  {item.text ?? item.title}
                </div>
              )}
              {item.reason ? <p className="subtle mt-1 text-[12.5px]">{item.reason}</p> : null}
            </li>
          );
        })}
      </ul>
    );
  }

  if (section.renderer === "formula_steps") {
    const steps = Array.isArray(content) ? (content as CalculationStep[]) : [];
    if (!steps.length) return null;
    return <CalculationSteps steps={steps} />;
  }

  if (section.renderer === "ordered_steps") {
    const steps = Array.isArray(content) ? (content as unknown[]) : [];
    return (
      <ol className="list-decimal space-y-1 pl-5 text-[13.5px]">
        {steps.map((item, index) => (
          <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>
        ))}
      </ol>
    );
  }

  if (section.renderer === "bullet_list") {
    const items = Array.isArray(content) ? (content as unknown[]) : [];
    if (!items.length) return null;
    return (
      <ul className="list-disc space-y-1 pl-5 text-[13.5px]">
        {items.map((item, index) => (
          <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>
        ))}
      </ul>
    );
  }

  if (Array.isArray(content)) {
    return (
      <ul className="list-disc space-y-1 pl-5 text-[13.5px]">
        {content.map((item, index) => (
          <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>
        ))}
      </ul>
    );
  }
  return <p className="whitespace-pre-wrap text-[13.5px] leading-7">{String(content ?? "")}</p>;
}

// 计算过程：公式 → 代入 → 结果 → 依据，一行一步。
// 三步都摆出来，用户才能自己核对；只给结果等于要求用户无条件相信系统。
export function CalculationSteps({ steps }: { steps: CalculationStep[] }) {
  return (
    <ol className="space-y-2">
      {steps.map((step, index) => (
        <li
          key={`${step.title}-${index}`}
          className="rounded border px-3 py-2"
          style={{ borderColor: "var(--border)", background: "var(--surface-muted)" }}
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-[13px] font-medium">
              {index + 1}. {step.title}
            </span>
            <span className="tabular text-[14px] font-semibold">{step.result}</span>
          </div>
          <div className="muted mt-0.5 text-[12.5px]">
            <span>{step.formula}</span>
            {step.substitution ? <span className="tabular">　代入 {step.substitution}</span> : null}
          </div>
          {step.citation ? <div className="subtle mt-0.5 text-[12px]">依据：{step.citation}</div> : null}
        </li>
      ))}
    </ol>
  );
}

// 「政策依据」段给的是文字标识（文号+条款号），要把点击行为接回真正的引用对象。
// 匹配不上就退化成纯文本——宁可少一个点击，也不要让用户点开一条对不上的条文。
function matchCitation(text: string, citations: Citation[]): Citation | null {
  if (!text) return null;
  const normalized = text.replace(/\s+/g, "");
  return (
    citations.find((item) => item.label.replace(/\s+/g, "") === normalized) ??
    citations.find((item) => item.regulation_title?.replace(/\s+/g, "") === normalized) ??
    citations.find(
      (item) =>
        normalized.includes(item.full_no ?? "\u0000") &&
        item.full_no !== null &&
        item.full_no !== "",
    ) ??
    null
  );
}

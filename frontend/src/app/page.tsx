"use client";

import { useState } from "react";

import { AnswerView } from "@/components/answer/AnswerView";
import { ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatDateTime } from "@/lib/api";
import type { AskResponse } from "@/lib/types";

// 财税问答页。
//
// 一次提问的完整呈现：判定 → 依据（可点开原文）→ 计算过程（分步）
// → 风险 → 说明，最后永远带合规信息条。
const EXAMPLES = [
  "我是小规模纳税人，上个月在市区卖了10万元含税货物，怎么交税",
  "小微企业有什么税收优惠",
  "我这个月进项税能不能抵扣",
  "发票丢了怎么办",
];

export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);
  const [feedback, setFeedback] = useState("");

  async function ask(text?: string) {
    const target = (text ?? question).trim();
    if (!target) return;
    setBusy(true);
    setError("");
    setFeedback("");
    setResult(null);
    try {
      const data = await api.ask({
        question: target,
        as_of: asOf ? `${asOf}T00:00:00Z` : null,
      });
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提问失败");
    } finally {
      setBusy(false);
    }
  }

  async function sendFeedback(helpful: boolean) {
    if (!result) return;
    try {
      const data = await api.feedback({ trace_id: result.compliance.trace_id, helpful });
      setFeedback(data.message);
    } catch (err) {
      setFeedback(err instanceof Error ? err.message : "反馈提交失败");
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
      <div>
        <SectionTitle level={1} hint="答案按六段式给出：情形判定、政策依据、计算过程、办理步骤、风险提示、说明。">
          问一个财税问题
        </SectionTitle>

        <form
          className="card p-4"
          onSubmit={(event) => {
            event.preventDefault();
            void ask();
          }}
        >
          <label className="label" htmlFor="question">
            您的问题
          </label>
          <textarea
            id="question"
            className="field"
            rows={3}
            value={question}
            placeholder="例如：我是小规模纳税人，上个月在市区卖了10万元含税货物，怎么交税"
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              // Ctrl/Cmd + Enter 提交：长文本里回车要留给换行
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void ask();
            }}
          />
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <div>
              <label className="label" htmlFor="as-of">
                按哪个时点回答（可留空=现在）
              </label>
              <input
                id="as-of"
                type="date"
                className="field w-[180px]"
                value={asOf}
                onChange={(event) => setAsOf(event.target.value)}
              />
            </div>
            <button type="submit" className="btn btn-primary" disabled={busy || !question.trim()}>
              {busy ? "检索与判定中…" : "提问"}
            </button>
            <span className="subtle text-[12px]">Ctrl / ⌘ + Enter 也可提交</span>
          </div>
        </form>

        <div className="mt-4 space-y-4">
          {busy ? <Loading label="正在检索条文、判定适用性并核对数字…" /> : null}
          {error ? <ErrorState message={error} onRetry={() => void ask()} /> : null}
          {result ? <ResultPanel result={result} feedback={feedback} onFeedback={sendFeedback} /> : null}
        </div>
      </div>

      <aside className="space-y-4">
        <div className="card p-3">
          <h2 className="text-[13px] font-semibold">试试这些</h2>
          <ul className="mt-2 space-y-1.5">
            {EXAMPLES.map((item) => (
              <li key={item}>
                <button
                  type="button"
                  className="text-left text-[12.5px] underline"
                  style={{ color: "var(--accent)" }}
                  onClick={() => {
                    setQuestion(item);
                    void ask(item);
                  }}
                >
                  {item}
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="card p-3 text-[12.5px] leading-6">
          <h2 className="text-[13px] font-semibold">这次回答是怎么来的</h2>
          <ol className="muted mt-1 list-decimal space-y-0.5 pl-4">
            <li>把问题拆成要素（主体、税种、金额、期间）</li>
            <li>三路检索现行有效条文（废止的进不来）</li>
            <li>逐条判定适用 / 不适用 / 需确认</li>
            <li>需要算钱时由计算引擎给步骤</li>
            <li>验证层核对引用、时效、数字、免责</li>
          </ol>
        </div>
      </aside>
    </div>
  );
}

function ResultPanel({
  result,
  feedback,
  onFeedback,
}: {
  result: AskResponse;
  feedback: string;
  onFeedback: (helpful: boolean) => void;
}) {
  return (
    <article className="card p-4" aria-live="polite">
      <header className="flex flex-wrap items-center gap-2 border-b pb-2" style={{ borderColor: "var(--border)" }}>
        <span className="tag tag-accent">意图：{intentLabel(result.intent)}</span>
        <span className="tag">模板：{result.template_id || "—"}</span>
        {result.refused ? <span className="tag tag-danger">未给结论</span> : null}
        {result.degraded.length ? (
          <span className="tag tag-warning">降级：{result.degraded.join("、")}</span>
        ) : null}
      </header>

      {result.next_questions.length ? (
        <div className="mt-3 rounded border p-3" style={{ borderColor: "#e6cd9a", background: "var(--warning-soft)" }}>
          <h2 className="text-[13px] font-semibold text-[var(--warning)]">需要您补充</h2>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-[13px]">
            {result.next_questions.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-4">
        {result.answer ? (
          <AnswerView answer={result.answer} citations={result.citations} />
        ) : (
          <p className="text-[13px] text-[var(--danger)]">未生成答案</p>
        )}
      </div>

      <ComplianceBar result={result} feedback={feedback} onFeedback={onFeedback} />
    </article>
  );
}

// 合规展示：免责声明、知识截至时间、验证结论、留痕编号，
// 每次都出现。其中"留痕编号"是给客服和审计用的——用户报问题时
// 报这个号，我们能精准定位到当时那次回答。
function ComplianceBar({
  result,
  feedback,
  onFeedback,
}: {
  result: AskResponse;
  feedback: string;
  onFeedback: (helpful: boolean) => void;
}) {
  return (
    <footer className="mt-5 border-t pt-3" style={{ borderColor: "var(--border)" }}>
      <p className="subtle text-[12.5px] leading-6">{result.compliance.disclaimer}</p>
      <dl className="muted mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[12px]">
        <div>
          <dt className="inline">知识截至：</dt>
          <dd className="inline">{formatDateTime(result.compliance.knowledge_as_of)}</dd>
        </div>
        <div>
          <dt className="inline">回答时间：</dt>
          <dd className="inline">{formatDateTime(result.compliance.answer_generated_at)}</dd>
        </div>
        <div>
          <dt className="inline">验证：</dt>
          <dd className="inline">{result.compliance.verified ? "已通过验证层" : "未开启验证"}</dd>
        </div>
        <div>
          <dt className="inline">留痕编号：</dt>
          <dd className="tabular inline">{result.compliance.trace_id}</dd>
        </div>
      </dl>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[12.5px]">
        <span className="muted">这个回答有用吗？</span>
        <button type="button" className="btn" onClick={() => onFeedback(true)}>
          有用
        </button>
        <button type="button" className="btn btn-danger" onClick={() => onFeedback(false)}>
          不准确
        </button>
        {feedback ? (
          <span role="status" className="subtle">
            {feedback}
          </span>
        ) : null}
      </div>
    </footer>
  );
}

function intentLabel(intent: string): string {
  const map: Record<string, string> = {
    calculation: "算税",
    how_to: "怎么办",
    inspection: "稽查",
    dispute: "争议",
    planning: "筹划",
    policy_query: "政策查询",
  };
  return map[intent] ?? intent;
}

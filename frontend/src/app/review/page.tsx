"use client";

import { useCallback, useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatMoney } from "@/lib/api";
import { RISK_LABEL, RISK_TONE, type ReviewItem, type ReviewWorkload } from "@/lib/types";

// 专家复核台。
//
// 一屏看清一条待办：画像 + 方案 + 依据 + 测算 + 风险点，然后三个动作
// 放行 / 修改后放行 / 驳回。驳回必须写原因——没有原因的驳回，
// 系统学不到东西，用户也拿不到替代方案。
const ACTIONS = [
  { value: "approve", label: "放行" },
  { value: "approve_with_changes", label: "修改后放行" },
  { value: "reject", label: "驳回" },
];

export default function ReviewPage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [workload, setWorkload] = useState<ReviewWorkload | null>(null);
  const [feedback, setFeedback] = useState<Array<Record<string, unknown>>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [action, setAction] = useState("approve");
  const [note, setNote] = useState("");
  const [riskLevel, setRiskLevel] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [list, stats, fb] = await Promise.all([
        api.reviews(),
        api.reviewWorkload(),
        api.reviewFeedback(),
      ]);
      setItems(list.items);
      setWorkload(stats);
      setFeedback(fb.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取复核队列失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const active = items.find((item) => item.id === activeId) ?? null;

  async function decide() {
    if (!active) return;
    setMessage("");
    try {
      await api.decideReview(active.id, {
        action,
        note,
        risk_level: riskLevel || null,
      });
      setMessage(`已提交：${active.plan?.name ?? active.id}`);
      setActiveId(null);
      setNote("");
      setRiskLevel("");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "提交失败");
    }
  }

  return (
    <div>
      <SectionTitle
        level={1}
        hint="高风险方案只到这里，不会出现在客户视图。驳回与修改会进入待转评测集的清单，供后续校准。"
      >
        专家复核台
      </SectionTitle>

      {workload ? (
        <div className="card mb-4 flex flex-wrap gap-x-6 gap-y-2 p-3 text-[13px]">
          <span>
            待办 <strong className="tabular">{workload.pending}</strong> 条
          </span>
          <span>
            已处理 <strong className="tabular">{workload.decided}</strong> 条
          </span>
          <span>
            平均处理{" "}
            <strong className="tabular">
              {workload.avg_hours === null ? "—" : workload.avg_hours}
            </strong>{" "}
            小时
          </span>
          <span>
            按时率{" "}
            <strong className="tabular">
              {workload.on_time_rate === null ? "—" : `${Math.round(workload.on_time_rate * 100)}%`}
            </strong>
            （SLA {workload.sla_hours} 小时）
          </span>
          {workload.overdue.length ? (
            <span className="text-[var(--danger)]">超时未处理 {workload.overdue.length} 条</span>
          ) : null}
        </div>
      ) : null}

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {message ? (
        <p role="status" className="mb-3 text-[13px] text-[var(--accent)]">
          {message}
        </p>
      ) : null}

      {!loading && !error && !items.length ? (
        <EmptyState
          title="当前没有待复核的筹划方案"
          hint="只有被判定为需要人工确认的方案才会进这里；等有黄色或红色方案时会自动出现。"
        />
      ) : null}

      {items.length ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
          <ul className="space-y-2">
            {items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className="card w-full p-3 text-left"
                  style={{
                    borderColor: item.id === activeId ? "var(--accent)" : "var(--border)",
                    background: item.id === activeId ? "var(--accent-soft)" : "var(--surface)",
                  }}
                  onClick={() => {
                    setActiveId(item.id);
                    setMessage("");
                  }}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`tag ${RISK_TONE[item.risk_level] ?? "tag"}`}>
                      {RISK_LABEL[item.risk_level] ?? item.risk_level}
                    </span>
                    <span className="text-[13.5px] font-medium">{item.plan?.name ?? "（无名称）"}</span>
                    {!item.delivered_to_user ? <span className="tag tag-danger">未发给用户</span> : null}
                  </div>
                  <p className="muted mt-1 line-clamp-2 text-[12.5px]">
                    诉求：{item.question || "（未记录）"}
                  </p>
                </button>
              </li>
            ))}
          </ul>

          {active ? (
            <div className="card h-fit p-4">
              <h2 className="text-[14px] font-semibold">一屏判定</h2>
              <p className="muted mt-1 text-[13px]">{active.plan?.name}</p>

              <dl className="mt-3 space-y-1.5 text-[13px]">
                <Row label="机制">{active.plan?.mechanism || "—"}</Row>
                <Row label="依据">{(active.plan?.citations ?? []).join("；") || "—"}</Row>
                <Row label="测算">
                  {active.plan?.measurement?.computable
                    ? `${formatMoney(active.plan.measurement.before)} → ${formatMoney(
                        active.plan.measurement.after,
                      )}，节税 ${formatMoney(active.plan.measurement.saving)} 元/年`
                    : active.plan?.measurement?.note || "无法测算"}
                </Row>
                <Row label="适用条件">{(active.plan?.preconditions ?? []).join("；") || "—"}</Row>
                <Row label="滥用边界">
                  <span className="text-[var(--danger)]">
                    {(active.plan?.abuse_boundary ?? []).join("；") || "—"}
                  </span>
                </Row>
                <Row label="被否案例">{(active.plan?.rejected_cases ?? []).join("；") || "—"}</Row>
                <Row label="企业画像">
                  {Object.entries(active.profile ?? {})
                    .filter(([, value]) => value !== null && value !== "" && value !== undefined)
                    .slice(0, 8)
                    .map(([key, value]) => `${key}=${String(value)}`)
                    .join("，") || "—"}
                </Row>
              </dl>

              <div className="mt-4 border-t pt-3" style={{ borderColor: "var(--border)" }}>
                <span className="label">复核动作</span>
                <div className="flex flex-wrap gap-3">
                  {ACTIONS.map((item) => (
                    <label key={item.value} className="flex items-center gap-1.5 text-[13px]">
                      <input
                        type="radio"
                        name="action"
                        value={item.value}
                        checked={action === item.value}
                        onChange={() => setAction(item.value)}
                      />
                      {item.label}
                    </label>
                  ))}
                </div>

                {action !== "reject" ? (
                  <div className="mt-3">
                    <label className="label" htmlFor="risk-level">
                      调整风险等级（可选）
                    </label>
                    <select
                      id="risk-level"
                      className="field"
                      value={riskLevel}
                      onChange={(event) => setRiskLevel(event.target.value)}
                    >
                      <option value="">不调整</option>
                      <option value="green">稳妥</option>
                      <option value="yellow">审慎</option>
                      <option value="red">高风险</option>
                    </select>
                  </div>
                ) : null}

                <div className="mt-3">
                  <label className="label" htmlFor="note">
                    复核意见{action === "reject" ? "（驳回必须填写原因）" : ""}
                  </label>
                  <textarea
                    id="note"
                    className="field"
                    rows={3}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />
                </div>

                <button
                  type="button"
                  className={action === "reject" ? "btn btn-danger mt-3 w-full" : "btn btn-primary mt-3 w-full"}
                  onClick={() => void decide()}
                  disabled={action === "reject" && !note.trim()}
                >
                  提交复核结论
                </button>
              </div>
            </div>
          ) : (
            <EmptyState title="从左侧选一条待办" hint="选中后这里显示方案详情与判定表单。" />
          )}
        </div>
      ) : null}

      {feedback.length ? (
        <div className="card mt-6 p-4">
          <h2 className="text-[14px] font-semibold">待转评测集的反馈（{feedback.length} 条）</h2>
          <p className="subtle mt-1 text-[12.5px]">
            驳回与修改后放行的案例会整理到这里。系统**不会**自动把它们写进手法库——
            手法库是专家资产，要由人确认后才能收录。
          </p>
          <ul className="mt-2 space-y-1.5 text-[13px]">
            {feedback.slice(0, 10).map((item, index) => (
              <li key={index} className="rounded border px-3 py-2" style={{ borderColor: "var(--border)" }}>
                <span className="tag">{String(item.status ?? "")}</span>
                <span className="ml-2">{String(item.question ?? "").slice(0, 40)}</span>
                <p className="muted mt-0.5 text-[12.5px]">
                  {String(item.reason ?? "")} · 用途：{String(item.suggested_use ?? "")}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {workload?.overdue.length ? (
        <div className="card mt-4 p-4" style={{ borderColor: "#e6cd9a" }}>
          <h2 className="text-[14px] font-semibold text-[var(--warning)]">超时未处理</h2>
          <p className="subtle mt-1 text-[12.5px]">
            规格要求超时"自动降级标注"，不静默放行——所以它们会一直留在这里直到有人处理。
          </p>
          <ul className="mt-2 space-y-1 text-[13px]">
            {workload.overdue.map((item) => (
              <li key={item.id}>
                {item.plan?.name ?? item.id}
                <span className="muted">　诉求：{item.question || "（未记录）"}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[76px_minmax(0,1fr)] gap-2">
      <dt className="muted">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

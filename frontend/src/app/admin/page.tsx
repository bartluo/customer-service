"use client";

import { useCallback, useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatDateTime } from "@/lib/api";
import { EFFECT_STATUS_LABEL, type AuditLogItem, type ConsoleOverview } from "@/lib/types";

// 管理后台：知识管理、缺口清单、评测报告、监控四个模块。
//
// 一条看板原则：**不用假数据填充**。依赖读不到时显示"查不到"并说明原因，
// 而不是显示 0——0 和"读不到"在界面上长得一样，但一个说明知识库空了，
// 一个说明系统有故障，混起来会误导判断。
type Tab = "knowledge" | "gaps" | "evaluations" | "monitor";

const TABS: Array<{ key: Tab; label: string }> = [
  { key: "knowledge", label: "知识管理" },
  { key: "gaps", label: "缺口清单" },
  { key: "evaluations", label: "评测报告" },
  { key: "monitor", label: "运行监控" },
];

export default function AdminPage() {
  const [tab, setTab] = useState<Tab>("knowledge");
  const [overview, setOverview] = useState<ConsoleOverview | null>(null);
  const [gaps, setGaps] = useState<Array<Record<string, unknown>>>([]);
  const [evaluations, setEvaluations] = useState<{
    runs: Array<Record<string, unknown>>;
    decisions: Array<Record<string, unknown>>;
  }>({ runs: [], decisions: [] });
  const [logs, setLogs] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [ov, gapData, evalData, logData] = await Promise.all([
        api.overview(),
        api.gaps(),
        api.evaluations(),
        api.auditLogs({ limit: 50 }),
      ]);
      setOverview(ov);
      setGaps(gapData.items);
      setEvaluations(evalData);
      setLogs(logData.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取后台数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <SectionTitle level={1} hint="知识规模、知识缺口、评测与门禁、运行留痕，四个模块共用一份数据源。">
        管理后台
      </SectionTitle>

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}

      {overview ? (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="法规总数" value={overview.regulations_total} />
          <Metric label="条文总数" value={overview.articles_total} />
          <Metric
            label="向量索引点"
            value={overview.index_points === null ? "查不到" : overview.index_points}
            tone={overview.index_points === null ? "danger" : undefined}
          />
          <Metric
            label="开放缺口"
            value={overview.open_gaps}
            tone={overview.open_gaps > 0 ? "warning" : undefined}
          />
          {overview.degraded.length ? (
            <p className="text-[12.5px] text-[var(--danger)] sm:col-span-2 lg:col-span-4">
              有依赖没读到：{overview.degraded.join("，")}——界面上的"—"表示读不到，不是 0。
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="mb-3 flex flex-wrap gap-1 border-b" style={{ borderColor: "var(--border)" }}>
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            aria-current={tab === item.key ? "true" : undefined}
            className="rounded-t px-3 py-1.5 text-[13px]"
            style={{
              background: tab === item.key ? "var(--surface)" : "transparent",
              border: tab === item.key ? "1px solid var(--border)" : "1px solid transparent",
              borderBottomColor: tab === item.key ? "var(--surface)" : "transparent",
              marginBottom: "-1px",
              fontWeight: tab === item.key ? 600 : 400,
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "knowledge" && overview ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <DistCard title="按效力状态" data={overview.by_effect_status} labelMap={EFFECT_STATUS_LABEL} />
          <DistCard
            title="按复核状态"
            data={overview.by_review_state}
            labelMap={{ published: "已发布", pending_review: "待复核", rejected: "已驳回" }}
          />
          <div className="card p-4 sm:col-span-2">
            <h3 className="text-[13.5px] font-semibold">知识运维动作</h3>
            <ul className="muted mt-1 list-disc space-y-1 pl-5 text-[12.5px] leading-6">
              <li>导入法规：命令行 <code>python scripts/import_regulations.py</code> 或接口 /api/knowledge/regulations/import</li>
              <li>审核放行：接口 /api/knowledge/review/pending 列出待复核，放行后自动重建索引</li>
              <li>重建全量索引：<code>docker compose exec backend python /app/scripts/build_vector_index.py</code></li>
              <li>单份法规索引随放行/废止即时重建（秒级），不需要等全量重建</li>
            </ul>
          </div>
        </div>
      ) : null}

      {tab === "gaps" ? (
        gaps.length ? (
          <div className="card overflow-x-auto p-4">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-left">
                  {["主题", "税种", "原因", "出现次数", "样例问题"].map((head) => (
                    <th key={head} className="border-b py-1.5 pr-3 font-medium" style={{ borderColor: "var(--border)" }}>
                      {head}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {gaps.map((row, index) => (
                  <tr key={String(row.id ?? index)}>
                    <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                      {String(row.topic ?? "")}
                    </td>
                    <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                      {String(row.tax_type ?? "—")}
                    </td>
                    <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                      {String(row.reason ?? "")}
                    </td>
                    <td className="tabular border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                      {String(row.occurrences ?? 0)}
                    </td>
                    <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                      {Array.isArray(row.samples) ? (row.samples as string[]).slice(0, 2).join("；") : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="目前没有开放的知识缺口" hint="缺口来自验证记录与用户反馈，出现后会自动汇总到这里。" />
        )
      ) : null}

      {tab === "evaluations" ? (
        <div className="space-y-3">
          {evaluations.decisions.length ? (
            <div className="card overflow-x-auto p-4">
              <h3 className="text-[13.5px] font-semibold">门禁结论</h3>
              <table className="mt-2 w-full border-collapse text-[13px]">
                <thead>
                  <tr className="text-left">
                    {["时间", "闸门", "结论", "一票否决", "是否回滚"].map((head) => (
                      <th key={head} className="border-b py-1.5 pr-3 font-medium" style={{ borderColor: "var(--border)" }}>
                        {head}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {evaluations.decisions.map((row, index) => {
                    const veto = Array.isArray(row.veto_items) ? (row.veto_items as string[]) : [];
                    return (
                      <tr key={String(row.id ?? index)}>
                        <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                          {formatDateTime(row.created_at as string)}
                        </td>
                        <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                          {String(row.gate ?? "")}
                        </td>
                        <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                          <span className={row.decision === "released" ? "tag tag-success" : "tag tag-danger"}>
                            {row.decision === "released"
                              ? "放行"
                              : row.decision === "rolled_back"
                                ? "已回滚"
                                : "阻断"}
                          </span>
                        </td>
                        <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                          {veto.length ? <span className="tag tag-danger">{veto.join("；")}</span> : "—"}
                        </td>
                        <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                          {row.rolled_back ? "是" : "否"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}

          {evaluations.runs.length ? (
            <div className="card p-4">
              <h3 className="text-[13.5px] font-semibold">最近评测</h3>
              <ul className="mt-2 space-y-3">
                {evaluations.runs.map((run, index) => {
                  const metrics = (run.metrics ?? {}) as Record<string, unknown>;
                  return (
                    <li key={String(run.id ?? index)} className="rounded border p-3" style={{ borderColor: "var(--border)" }}>
                      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                        <span className="muted">{formatDateTime(run.finished_at as string)}</span>
                        <span className="tag">{String(run.gate ?? "")}</span>
                        <span className="tabular">
                          通过 {String(run.passed_cases ?? 0)}/{String(run.total_cases ?? 0)}
                        </span>
                      </div>
                      <dl className="muted mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[12.5px]">
                        {Object.entries(metrics).map(([key, value]) => (
                          <div key={key} className="flex gap-1">
                            <dt>{key}</dt>
                            <dd className="tabular">{value === null ? "—" : String(value)}</dd>
                          </div>
                        ))}
                      </dl>
                    </li>
                  );
                })}
              </ul>
              <p className="subtle mt-3 text-[12.5px]">
                红线指标（废止陷阱泄漏数）要求为 0；它不参与加权打分，
                出现一条就整批阻断。
              </p>
            </div>
          ) : (
            <EmptyState title="还没有评测记录" hint="执行 python scripts/evaluate.py run 或 gate 后会出现在这里。" />
          )}
        </div>
      ) : null}

      {tab === "monitor" ? (
        <div className="space-y-3">
          <div className="card p-4">
            <h3 className="text-[13.5px] font-semibold">系统开关</h3>
            <dl className="mt-1.5 space-y-1 text-[13px]">
              <div className="flex gap-2">
                <dt className="muted">筹划功能</dt>
                <dd>
                  <span className={overview?.planning_enabled ? "tag tag-success" : "tag tag-warning"}>
                    {overview?.planning_enabled ? "已开启" : "未开启（资质主体落实前保持关闭）"}
                  </span>
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="muted">待复核筹划方案</dt>
                <dd className="tabular">{overview?.pending_reviews ?? "—"} 条</dd>
              </div>
            </dl>
          </div>

          <div className="card overflow-x-auto p-4">
            <h3 className="text-[13.5px] font-semibold">审计留痕（最近 50 条）</h3>
            <p className="subtle mt-1 text-[12.5px]">
              审计日志只增不改。问过什么、放行了什么、谁做的，都在这里。
            </p>
            <table className="mt-2 w-full border-collapse text-[12.5px]">
              <thead>
                <tr className="text-left">
                  {["时间", "操作人", "动作", "对象"].map((head) => (
                    <th key={head} className="border-b py-1.5 pr-3 font-medium" style={{ borderColor: "var(--border)" }}>
                      {head}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {logs.map((row) => (
                  <tr key={row.id}>
                    <td className="muted border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                      {formatDateTime(row.occurred_at)}
                    </td>
                    <td className="border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                      {row.actor_username ?? "—"}
                    </td>
                    <td className="border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                      {row.action}
                    </td>
                    <td className="muted border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                      {row.target_type ? `${row.target_type} ${row.target_id ?? ""}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: "danger" | "warning";
}) {
  const color = tone === "danger" ? "var(--danger)" : tone === "warning" ? "var(--warning)" : "var(--text)";
  return (
    <div className="card p-3">
      <p className="muted text-[12.5px]">{label}</p>
      <p className="tabular mt-0.5 text-[19px] font-semibold" style={{ color }}>
        {value}
      </p>
    </div>
  );
}

function DistCard({
  title,
  data,
  labelMap,
}: {
  title: string;
  data: Record<string, number>;
  labelMap: Record<string, string>;
}) {
  const entries = Object.entries(data);
  return (
    <div className="card p-4">
      <h3 className="text-[13.5px] font-semibold">{title}</h3>
      <ul className="mt-1.5 space-y-1 text-[13px]">
        {entries.map(([key, count]) => (
          <li key={key} className="flex items-center justify-between">
            <span>{labelMap[key] ?? key}</span>
            <span className="tabular">{count}</span>
          </li>
        ))}
        {!entries.length ? <li className="subtle">暂无数据</li> : null}
      </ul>
    </div>
  );
}

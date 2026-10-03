"use client";

import { useCallback, useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatDateTime } from "@/lib/api";
import type { PolicyChangeResponse } from "@/lib/types";

// 政策变化提醒页。
//
// 两处刻意不粉饰：
//   1. 推送通道没配置时，页面上直接写"未配置推送通道"——
//      显示"已推送"会让用户以为订阅生效了，等政策真变了却没收到，损失更大；
//   2. 变更按影响程度排序（废止/修订 > 新增场景 > 解读文章），
//      而不是按时间倒序——时间倒序里一条无关的解读会盖住一条废止通知。
const IMPORTANCE_TONE: Record<string, string> = {
  repealed: "tag-danger",
  amended: "tag-warning",
  new_regulation: "tag-accent",
  interpretation: "tag",
};

const TYPE_LABEL: Record<string, string> = {
  repealed: "法规废止",
  amended: "法规修订",
  new_regulation: "新增文件",
  interpretation: "政策解读",
};

export default function PolicyChangesPage() {
  const [data, setData] = useState<PolicyChangeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await api.policyChanges());
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取政策变化失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.digest.items ?? [];

  return (
    <div>
      <SectionTitle
        level={1}
        hint="企业最怕的是不知道政策变了。这里按对您的实际影响排序，而不是按发布时间。"
      >
        政策变化提醒
      </SectionTitle>

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}

      {data ? (
        <>
          <div className="card mb-4 p-3 text-[13px]">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
              <span>
                共 <strong className="tabular">{data.digest.count ?? 0}</strong> 条变更
              </span>
              <span className="muted">生成时间：{formatDateTime(data.digest.generated_at)}</span>
            </div>
            <p className="mt-1.5 text-[12.5px] text-[var(--warning)]">
              推送状态：{data.digest.delivery ?? "未配置推送通道"}
            </p>
          </div>

          {items.length ? (
            <ul className="space-y-2">
              {items.map((item, index) => (
                <li key={index} className="card p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="tabular subtle text-[12.5px]">#{item.importance ?? index + 1}</span>
                    <span className={`tag ${IMPORTANCE_TONE[item.event_type ?? ""] ?? "tag"}`}>
                      {TYPE_LABEL[item.event_type ?? ""] ?? item.event_type ?? "变更"}
                    </span>
                    <span className="text-[13.5px] font-medium">{item.title}</span>
                  </div>
                  {item.document_number ? (
                    <p className="muted mt-1 text-[12.5px]">{item.document_number}</p>
                  ) : null}
                  <p className="mt-1 text-[13px]">{item.note}</p>
                  {item.affected?.length ? (
                    <p className="mt-1 text-[12.5px]">
                      受影响：
                      {item.affected.slice(0, 5).map((name, idx) => (
                        <span key={idx} className="tag mr-1">
                          {name}
                        </span>
                      ))}
                    </p>
                  ) : null}
                  {item.discovered_at ? (
                    <p className="subtle mt-1 text-[12px]">发现时间：{formatDateTime(item.discovered_at)}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="近期没有政策变更记录"
              hint="监控会轮询官方栏目；发现新文件、废止或修订时会写入这里。"
            />
          )}

          {data.events.length ? (
            <div className="card mt-4 overflow-x-auto p-4">
              <h2 className="text-[13.5px] font-semibold">原始变更事件（{data.events.length} 条）</h2>
              <table className="mt-2 w-full border-collapse text-[12.5px]">
                <thead>
                  <tr className="text-left">
                    {["发现时间", "类型", "标题", "状态"].map((head) => (
                      <th key={head} className="border-b py-1.5 pr-3 font-medium" style={{ borderColor: "var(--border)" }}>
                        {head}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.events.map((row, index) => (
                    <tr key={String(row.id ?? index)}>
                      <td className="muted border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                        {formatDateTime((row.discovered_at ?? row.created_at) as string)}
                      </td>
                      <td className="border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                        {TYPE_LABEL[String(row.event_type)] ?? String(row.event_type ?? "")}
                      </td>
                      <td className="border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                        {String(row.title ?? "")}
                      </td>
                      <td className="muted border-b py-1 pr-3" style={{ borderColor: "var(--border)" }}>
                        {String(row.status ?? "")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { EFFECT_STATUS_LABEL, EFFECT_STATUS_TONE, type Citation, type CitationDetail } from "@/lib/types";

// 引用卡片：点开就能看到原文、条款号、效力状态、生效区间。
//
// 为什么要把"效力状态"顶在最显眼的位置：财税场景里最危险的错误不是
// "引错条"，而是"引了一条已经废止的条"。原文看起来一模一样的，
// 状态才是决定能不能用的那一条信息。
export function CitationCard({ citation }: { citation: Citation }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<CitationDetail | null>(null);
  const [error, setError] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open || !citation.article_version_id) return;
    let alive = true;
    setError("");
    api
      .citation(citation.article_version_id)
      .then((data) => {
        if (alive) setDetail(data);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "读取原文失败");
      });
    return () => {
      alive = false;
    };
  }, [open, citation.article_version_id]);

  // 打开后把焦点移进弹层；关闭后还给触发按钮。
  // 不做这一步，键盘用户关掉弹层后焦点会掉回页面开头。
  useEffect(() => {
    if (open) closeRef.current?.focus();
    else triggerRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const tone = EFFECT_STATUS_TONE[citation.effect_status] ?? "tag";
  const statusLabel = EFFECT_STATUS_LABEL[citation.effect_status] ?? citation.effect_status;
  const clickable = Boolean(citation.article_version_id);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="w-full rounded border px-3 py-2 text-left transition-colors hover:bg-[var(--surface-muted)]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        onClick={() => setOpen(true)}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-semibold">{citation.regulation_title || "（未标注名称）"}</span>
          <span className={`tag ${tone}`}>{statusLabel}</span>
          {!citation.traceable ? <span className="tag tag-warning">引用不完整</span> : null}
        </div>
        <div className="muted mt-1 flex flex-wrap items-center gap-x-3 text-[12.5px]">
          <span>{citation.document_number || "无文号"}</span>
          <span>{citation.full_no || "未定位到条款"}</span>
          <span className="subtle">
            匹配度 {Math.round((citation.score ?? 0) * 100)}%
            {citation.routes?.length ? ` · 命中路径 ${citation.routes.join("/")}` : ""}
          </span>
        </div>
        <p className="mt-1.5 line-clamp-2 text-[13px]">{citation.content}</p>
        <span className="subtle mt-1 inline-block text-[12px]">
          {clickable ? "点击查看原文与效力状态" : "该条缺少版本标识，无法展开原文"}
        </span>
      </button>

      {open ? (
        <div
          className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/35 p-4"
          onClick={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="引用原文"
            className="card mt-8 w-full max-w-[720px] p-4"
            style={{ boxShadow: "0 8px 28px rgba(16, 24, 32, 0.18)" }}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <h3 className="text-[15px] font-semibold">
                  {detail?.regulation_title ?? citation.regulation_title}
                </h3>
                <p className="muted mt-0.5 text-[12.5px]">
                  {(detail?.document_number ?? citation.document_number) || "无文号"}
                  {" · "}
                  {detail?.full_no ?? citation.full_no}
                  {detail?.issuer ? ` · ${detail.issuer}` : ""}
                </p>
              </div>
              <button
                ref={closeRef}
                type="button"
                className="btn"
                onClick={() => setOpen(false)}
                aria-label="关闭引用详情"
              >
                关闭
              </button>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className={`tag ${tone}`}>效力状态：{statusLabel}</span>
              {detail?.repealed ? (
                <span className="tag tag-danger">此条文已不可作为依据引用</span>
              ) : null}
              {detail ? (
                <span className="tag">
                  生效：{detail.valid_from ? detail.valid_from.slice(0, 10) : "未记录"}
                  {" → "}
                  {detail.valid_to ? detail.valid_to.slice(0, 10) : "至今"}
                </span>
              ) : null}
            </div>

            <div className="mt-3">
              {error ? <p className="text-[13px] text-[var(--danger)]">{error}</p> : null}
              {!detail && !error ? <p className="subtle text-[13px]">正在读取原文…</p> : null}
              {detail ? (
                <p className="whitespace-pre-wrap rounded border p-3 text-[13.5px] leading-7"
                   style={{ borderColor: "var(--border)", background: "var(--surface-muted)" }}>
                  {detail.content}
                </p>
              ) : null}
            </div>

            {detail?.repeal_basis ? (
              <p className="mt-2 text-[12.5px] text-[var(--danger)]">废止依据：{detail.repeal_basis}</p>
            ) : null}
            {detail?.amend_basis ? (
              <p className="mt-2 text-[12.5px] text-[var(--warning)]">修订依据：{detail.amend_basis}</p>
            ) : null}

            <div className="mt-3 flex flex-wrap gap-3 text-[12.5px]">
              {detail?.regulation_source_url ? (
                <a
                  className="underline"
                  style={{ color: "var(--accent)" }}
                  href={detail.regulation_source_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  打开官方来源
                </a>
              ) : null}
              {detail ? (
                <span className="subtle">条文版本 ID：{detail.article_version_id}</span>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

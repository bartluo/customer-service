"use client";

// 三个通用状态块。
//
// 为什么做成组件强制使用：空白页和"加载中"长得一样，用户分不清
// 是"没有数据"还是"页面坏了"。每个数据区都必须给出这三种状态之一。

export function Loading({ label = "加载中…" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="subtle py-6 text-center text-[13px]">
      {label}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="card px-4 py-3 text-[13px]"
      style={{ borderColor: "#e3b4b6", background: "var(--danger-soft)", color: "var(--danger)" }}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span>{message}</span>
        {onRetry ? (
          <button type="button" className="btn" onClick={onRetry}>
            重试
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="card subtle px-4 py-8 text-center text-[13px]">
      <p>{title}</p>
      {hint ? <p className="mt-1">{hint}</p> : null}
    </div>
  );
}

export function SectionTitle({
  children,
  hint,
  level = 2,
}: {
  children: React.ReactNode;
  hint?: string;
  level?: 1 | 2 | 3;
}) {
  const Tag = (level === 1 ? "h1" : level === 2 ? "h2" : "h3") as "h1" | "h2" | "h3";
  return (
    <div className="mb-3">
      <Tag className={level === 1 ? "text-[18px] font-semibold" : "text-[15px] font-semibold"}>
        {children}
      </Tag>
      {hint ? <p className="subtle mt-0.5 text-[12.5px]">{hint}</p> : null}
    </div>
  );
}

"use client";

/** 后端调用封装。
 *
 * 三件事必须集中在一处做，散在各个页面里迟早不一致：
 *   1. 带上登录令牌；
 *   2. 令牌失效（401）时清掉本地令牌并跳登录页——否则用户会看到
 *      一整屏"加载失败"，却不知道只要重新登录就好；
 *   3. 错误信息取后端返回的 detail 字段（FastAPI 的统一错误格式），
 *      而不是把 HTTP 状态码丢给用户看。
 */

import type {
  AskResponse,
  AuditLogItem,
  CitationDetail,
  ConsoleOverview,
  LoginResponse,
  PlanningAnalyzeResponse,
  PolicyChangeResponse,
  ReviewItem,
  ReviewWorkload,
  TaxCalcResponse,
} from "./types";

const TOKEN_KEY = "cs_access_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    // 网络层失败（后端没起、断网）也要给一句人话，不能只抛 TypeError
    throw new ApiError(0, "无法连接后端服务，请确认后端已启动");
  }

  if (response.status === 401) {
    clearToken();
    // 已在登录页时不要再跳，否则会自己打断自己
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
    throw new ApiError(401, "登录已失效，请重新登录");
  }

  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    const detail =
      (data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : "") || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export const api = {
  login: (username: string, password: string) =>
    request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  me: () => request<{ user: LoginResponse["user"] }>("/api/auth/me"),

  ask: (body: {
    question: string;
    tax_type?: string | null;
    as_of?: string | null;
    top_n?: number;
  }) => request<AskResponse>("/api/v1/qa/ask", { method: "POST", body: JSON.stringify(body) }),

  feedback: (body: {
    trace_id: string;
    helpful: boolean;
    reason?: string;
    corrected_answer?: string;
  }) =>
    request<{ trace_id: string; accepted: boolean; message: string }>("/api/v1/qa/feedback", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  citation: (articleVersionId: string) =>
    request<CitationDetail>(`/api/v1/citations/${encodeURIComponent(articleVersionId)}`),

  calcTax: (body: Record<string, unknown>) =>
    request<TaxCalcResponse>("/api/v1/calc/tax", { method: "POST", body: JSON.stringify(body) }),

  profileSchema: () => request<Record<string, unknown>>("/api/v1/planning/profile-schema"),

  analyze: (body: { profile: Record<string, unknown>; request_text?: string }) =>
    request<PlanningAnalyzeResponse>("/api/v1/planning/analyze", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  reviews: (limit = 50) =>
    request<{ total: number; items: ReviewItem[] }>(`/api/v1/planning/reviews?limit=${limit}`),

  reviewWorkload: () => request<ReviewWorkload>("/api/v1/planning/reviews/workload"),

  reviewFeedback: () =>
    request<{ total: number; items: Array<Record<string, unknown>> }>(
      "/api/v1/planning/reviews/feedback",
    ),

  decideReview: (
    reviewId: string,
    body: { action: string; note?: string; risk_level?: string | null },
  ) =>
    request<ReviewItem>(`/api/v1/planning/reviews/${encodeURIComponent(reviewId)}/decision`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  overview: () => request<ConsoleOverview>("/api/v1/console/overview"),

  gaps: (limit = 100) =>
    request<{ total: number; items: Array<Record<string, unknown>> }>(
      `/api/v1/console/gaps?limit=${limit}`,
    ),

  evaluations: (limit = 10) =>
    request<{ runs: Array<Record<string, unknown>>; decisions: Array<Record<string, unknown>> }>(
      `/api/v1/console/evaluations?limit=${limit}`,
    ),

  policyChanges: (limit = 50) =>
    request<PolicyChangeResponse>(`/api/v1/console/policy-changes?limit=${limit}`),

  auditLogs: (params: { limit?: number; action?: string } = {}) => {
    const search = new URLSearchParams();
    search.set("limit", String(params.limit ?? 50));
    if (params.action) search.set("action", params.action);
    return request<{ total: number; items: AuditLogItem[] }>(
      `/api/v1/console/audit-logs?${search.toString()}`,
    );
  },
};

/** 把 ISO 时间转成"2026-10-02 19:32"。表格里显示到分钟就够，
 *  多余的秒级精度只会让列变宽、更难扫。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** 金额显示：加千分位。财税界面上"1000000"和"1,000,000"的读错概率差很多。 */
export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return num.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

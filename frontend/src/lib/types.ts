/** 后端接口的数据结构（对应 backend/app/schemas）。
 *
 * 为什么手写而不自动生成：首版接口数量不多，手写能让"改了后端忘了改前端"
 * 这类问题在 typecheck 阶段暴露出来（字段名对不上就编译不过）。
 * 接口数量涨到几十个以后再引入 OpenAPI 代码生成。
 */

export type EffectStatus =
  | "effective"
  | "partially_repealed"
  | "repealed"
  | "superseded"
  | "draft"
  | "not_yet_effective";

export const EFFECT_STATUS_LABEL: Record<string, string> = {
  effective: "现行有效",
  partially_repealed: "部分失效",
  repealed: "已废止",
  superseded: "已被替代",
  draft: "草案",
  not_yet_effective: "尚未生效",
};

export const EFFECT_STATUS_TONE: Record<string, string> = {
  effective: "tag-success",
  partially_repealed: "tag-warning",
  repealed: "tag-danger",
  superseded: "tag-danger",
  draft: "tag",
  not_yet_effective: "tag-warning",
};

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: {
    id: string;
    username: string;
    display_name: string;
    tenant_id: string | null;
    is_protected: boolean;
    roles: string[];
    permissions: string[];
  };
}

export interface AnswerSection {
  key: string;
  title: string;
  /** text / citation_list / formula_steps / ordered_steps / bullet_list / fixed_text */
  renderer: string;
  content: unknown;
}

export interface AnswerBody {
  template_id: string;
  ok: boolean;
  failure_reason: string;
  missing_required: string[];
  sections: AnswerSection[];
}

export interface CalculationStep {
  title: string;
  formula: string;
  substitution: string;
  result: string;
  citation: string;
}

export interface Citation {
  label: string;
  identifier: string;
  document_number: string | null;
  full_no: string | null;
  regulation_title: string | null;
  issuer: string | null;
  hierarchy_level: string | null;
  level_code: string | null;
  effect_status: string;
  content: string;
  source_url: string | null;
  tax_types: string[];
  regulation_id: string | null;
  article_version_id: string | null;
  valid_from_ts: number | null;
  valid_to_ts: number | null;
  traceable: boolean;
  score: number;
  routes: string[];
  rerank_reason: string | null;
}

export interface ComplianceInfo {
  disclaimer: string;
  knowledge_as_of: string | null;
  answer_generated_at: string;
  trace_id: string;
  verified: boolean;
  refusal_reason: string;
}

export interface AskResponse {
  question: string;
  intent: string;
  template_id: string;
  answer: AnswerBody | null;
  citations: Citation[];
  facts: Record<string, unknown>;
  applicability: {
    applicable?: Array<{ title?: string; citation?: string; reason?: string }>;
    not_applicable?: Array<{ title?: string; citation?: string; reason?: string }>;
    need_confirm?: Array<{ title?: string; citation?: string; reason?: string }>;
  } | null;
  calculation: Record<string, unknown> | null;
  verification: Record<string, unknown> | null;
  refused: boolean;
  degraded: string[];
  compliance: ComplianceInfo;
  next_questions: string[];
}

export interface CitationDetail {
  article_version_id: string;
  article_id: string;
  regulation_id: string;
  regulation_title: string;
  document_number: string | null;
  issuer: string | null;
  hierarchy_level: string | null;
  level_code: string | null;
  full_no: string;
  heading_path: string | null;
  content: string;
  effect_status: string;
  valid_from: string | null;
  valid_to: string | null;
  source_url: string | null;
  regulation_source_url: string | null;
  repealed: boolean;
  repeal_basis: string | null;
  amend_basis: string | null;
}

export interface CalcBlock {
  tax_type: string;
  method: string;
  payable: string;
  steps: CalculationStep[];
  notes: string[];
  citations: string[];
  discount_applied: string | null;
  rule_version: string;
  computable: boolean;
}

export interface TaxCalcResponse {
  vat: CalcBlock;
  surcharges: CalcBlock | null;
  total_payable: string;
  notes: string[];
  rule_version: string;
  disclaimer: string;
}

export interface PlanRecord {
  code: string;
  name: string;
  path: string;
  path_name: string;
  category: string;
  risk_level: string;
  mechanism: string;
  actions: string[];
  steps: string[];
  evidence: string[];
  cost: string;
  citations: string[];
  preconditions: string[];
  abuse_boundary: string[];
  rejected_cases: string[];
  measurement: {
    computable?: boolean;
    before?: string | null;
    after?: string | null;
    saving?: string | null;
    tax_type?: string;
    note?: string;
    steps?: CalculationStep[];
  };
  missing: string[];
}

export interface PlanningAnalyzeResponse {
  profile: Record<string, unknown>;
  missing_required: string[];
  planning_enabled: boolean;
  plans: PlanRecord[];
  withheld: PlanRecord[];
  comparison: { rows: Array<Record<string, string>>; combinations: string[] };
  legality: {
    passed?: boolean;
    fatal?: boolean;
    risk_level?: string;
    reasons?: string[];
    checks?: Array<{ name: string; passed: boolean; reason?: string }>;
    red_lines?: Array<Record<string, unknown>>;
  };
  risk_level: string;
  notes: string[];
  created_reviews: number;
  disclaimer: string;
}

export interface ReviewItem {
  id: string;
  question: string;
  risk_level: string;
  status: string;
  technique_code: string | null;
  delivered_to_user: boolean;
  profile: Record<string, unknown>;
  plan: PlanRecord;
}

export interface ReviewWorkload {
  pending: number;
  decided: number;
  /** 还没有已处理记录时为 null（未知，不是 0） */
  avg_hours: number | null;
  sla_hours: number;
  on_time_rate: number | null;
  overdue: ReviewItem[];
}

export interface ConsoleOverview {
  regulations_total: number;
  articles_total: number;
  index_points: number | null;
  by_effect_status: Record<string, number>;
  by_review_state: Record<string, number>;
  open_gaps: number;
  pending_reviews: number;
  latest_evaluation: Record<string, unknown> | null;
  latest_gate_decision: Record<string, unknown> | null;
  planning_enabled: boolean;
  degraded: string[];
}

export interface PolicyChangeResponse {
  digest: {
    generated_at?: string;
    count?: number;
    delivery?: string;
    items?: Array<{
      importance?: number;
      title?: string;
      document_number?: string | null;
      note?: string;
      affected?: string[];
      event_type?: string;
      discovered_at?: string | null;
    }>;
  };
  events: Array<Record<string, unknown>>;
}

export interface AuditLogItem {
  id: number;
  occurred_at: string;
  actor_username: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown>;
}

export const RISK_LABEL: Record<string, string> = {
  green: "稳妥",
  yellow: "审慎",
  red: "高风险",
};

export const RISK_TONE: Record<string, string> = {
  green: "tag-success",
  yellow: "tag-warning",
  red: "tag-danger",
};

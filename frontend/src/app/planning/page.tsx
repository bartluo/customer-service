"use client";

import { useState } from "react";

import { EmptyState, ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatMoney } from "@/lib/api";
import { RISK_LABEL, RISK_TONE, type PlanRecord, type PlanningAnalyzeResponse } from "@/lib/types";

// 筹划方案对比台。
//
// 这一页最重要的是**把边界摆在方案旁边**：每个方案都带
// 「滥用边界」与「被否案例」——只讲怎么省税、不讲做到哪一步就违规，
// 等于把风险留给用户自己去撞。
//
// 高风险（🔴）方案单独放一块，并且只在有复核权限时显示；
// 它们本来就不该出现在给客户的视图里。
const EMPTY_PROFILE = {
  entity_type: "有限公司",
  taxpayer_type: "小规模纳税人",
  industry: "软件和信息技术服务业",
  region: "市区",
  employees: "20",
  annual_revenue: "900000",
  revenue: "900000",
  cost: "600000",
  profit: "300000",
  taxable_income: "300000",
  total_assets: "2000000",
  goal: "降低税负",
  goal_detail: "希望在不改变业务的前提下降低综合税负",
  risk_preference: "稳健",
};

export default function PlanningPage() {
  const [form, setForm] = useState<Record<string, string>>({ ...EMPTY_PROFILE });
  const [flex, setFlex] = useState({ can_change_contract: true, can_change_entity: false, can_change_timing: true });
  const [purpose, setPurpose] = useState("为客户提供持续的软件运维服务，客户要求按月结算");
  const [benefit, setBenefit] = useState("按月结算降低客户资金压力，提高了续约率");
  const [authentic, setAuthentic] = useState(true);
  const [requestText, setRequestText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<PlanningAnalyzeResponse | null>(null);

  function update(key: string, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function analyze(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const profile: Record<string, unknown> = { ...form, flexible: flex };
      profile.business_purpose = purpose;
      profile.business_benefit = benefit;
      profile.business_authentic = authentic;
      // 空字符串会被后端当成"填了一个空值"，不如直接不传
      Object.keys(profile).forEach((key) => {
        if (profile[key] === "") delete profile[key];
      });
      const data = await api.analyze({ profile, request_text: requestText });
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <SectionTitle
        level={1}
        hint="筹划的前提是把边界讲清楚：系统在合法范围内找更优解，并明确标出哪一步会变成违规。"
      >
        筹划方案对比
      </SectionTitle>

      <div className="grid gap-6 lg:grid-cols-[400px_minmax(0,1fr)]">
        <form className="card space-y-3 p-4" onSubmit={analyze} aria-label="企业画像">
          <h2 className="text-[14px] font-semibold">企业画像</h2>
          <Field id="entity_type" label="组织形式" value={form.entity_type} onChange={update}>
            <option>有限公司</option>
            <option>股份有限公司</option>
            <option>合伙企业</option>
            <option>个体工商户</option>
            <option>个人独资企业</option>
          </Field>
          <Field id="taxpayer_type" label="纳税人身份" value={form.taxpayer_type} onChange={update}>
            <option>一般纳税人</option>
            <option>小规模纳税人</option>
          </Field>
          <TextField id="industry" label="行业" value={form.industry} onChange={update} />
          <TextField id="employees" label="员工人数" value={form.employees} onChange={update} money />
          <TextField id="revenue" label="年营业收入（元）" value={form.revenue} onChange={update} money />
          <TextField id="cost" label="年成本费用（元）" value={form.cost} onChange={update} money />
          <TextField id="profit" label="年利润（元）" value={form.profit} onChange={update} money />
          <TextField id="taxable_income" label="应纳税所得额（元）" value={form.taxable_income} onChange={update} money />

          <Field id="goal" label="筹划目标" value={form.goal} onChange={update}>
            <option>降低税负</option>
            <option>递延纳税</option>
            <option>合规整改</option>
            <option>特定事项</option>
          </Field>
          <Field id="risk_preference" label="风险偏好" value={form.risk_preference} onChange={update}>
            <option>保守</option>
            <option>稳健</option>
            <option>进取</option>
          </Field>

          <fieldset className="rounded border p-2.5" style={{ borderColor: "var(--border)" }}>
            <legend className="muted px-1 text-[12.5px]">可以调整什么</legend>
            {(
              [
                ["can_change_contract", "可以调整合同条款"],
                ["can_change_entity", "可以调整主体架构"],
                ["can_change_timing", "可以调整交易时点"],
              ] as const
            ).map(([key, label]) => (
              <label key={key} className="flex items-center gap-2 py-0.5 text-[13px]">
                <input
                  type="checkbox"
                  checked={flex[key]}
                  onChange={(event) => setFlex((prev) => ({ ...prev, [key]: event.target.checked }))}
                />
                {label}
              </label>
            ))}
          </fieldset>

          <TextField id="business_purpose" label="真实商业目的（与税无关的理由）" value={purpose} onChange={(_, v) => setPurpose(v)} />
          <TextField id="business_benefit" label="带来的经营效益" value={benefit} onChange={(_, v) => setBenefit(v)} />
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={authentic} onChange={(event) => setAuthentic(event.target.checked)} />
            我确认上述业务有真实业务实质
          </label>

          <div>
            <label className="label" htmlFor="request-text">
              您的原始诉求（用于红线检查）
            </label>
            <textarea
              id="request-text"
              className="field"
              rows={2}
              value={requestText}
              placeholder="例如：能不能通过拆分合同把收入做小"
              onChange={(event) => setRequestText(event.target.value)}
            />
          </div>

          <button type="submit" className="btn btn-primary w-full" disabled={busy}>
            {busy ? "生成中…" : "生成方案"}
          </button>
        </form>

        <div className="space-y-4">
          {busy ? <Loading label="正在匹配手法、测算税负、跑四道合法性检查…" /> : null}
          {error ? <ErrorState message={error} /> : null}
          {result ? <ResultPanel result={result} /> : (
            !busy && !error ? (
              <EmptyState title="填好画像后点「生成方案」" hint="系统会给出多条路径的方案、节税测算与风险分级。" />
            ) : null
          )}
        </div>
      </div>
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  children,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (key: string, value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <select id={id} className="field" value={value} onChange={(event) => onChange(id, event.target.value)}>
        {children}
      </select>
    </div>
  );
}

function TextField({
  id,
  label,
  value,
  onChange,
  money = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (key: string, value: string) => void;
  money?: boolean;
}) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={money ? "field tabular" : "field"}
        inputMode={money ? "decimal" : undefined}
        value={value}
        onChange={(event) => onChange(id, event.target.value)}
      />
    </div>
  );
}

function ResultPanel({ result }: { result: PlanningAnalyzeResponse }) {
  return (
    <>
      <div className="card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tag tag-accent">整体风险：{RISK_LABEL[result.risk_level] ?? result.risk_level}</span>
          <span className={result.planning_enabled ? "tag tag-success" : "tag tag-warning"}>
            筹划总开关：{result.planning_enabled ? "已开启" : "未开启"}
          </span>
          <span className="tag">对外方案 {result.plans.length} 个</span>
          {result.withheld.length ? <span className="tag tag-danger">拦截 {result.withheld.length} 个</span> : null}
          {result.created_reviews ? <span className="tag">已送复核 {result.created_reviews} 条</span> : null}
        </div>
        {result.notes.length ? (
          <ul className="mt-2 list-disc space-y-1 pl-5 text-[12.5px] text-[var(--warning)]">
            {result.notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        ) : null}
        {result.missing_required.length ? (
          <p className="mt-2 text-[12.5px] text-[var(--danger)]">
            还缺关键信息：{result.missing_required.join("；")}
          </p>
        ) : null}
      </div>

      <div className="card p-4">
        <h2 className="text-[14px] font-semibold">四道合法性检查</h2>
        <ul className="mt-2 grid gap-2 sm:grid-cols-2">
          {(result.legality.checks ?? []).map((check) => (
            <li key={check.name} className="rounded border px-3 py-2 text-[13px]" style={{ borderColor: "var(--border)" }}>
              <span className={check.passed ? "tag tag-success" : "tag tag-danger"}>
                {check.passed ? "通过" : "未通过"}
              </span>
              <span className="ml-2 font-medium">{check.name}</span>
              {check.reason ? <p className="muted mt-1 text-[12.5px]">{check.reason}</p> : null}
            </li>
          ))}
        </ul>
      </div>

      {result.comparison.rows.length ? (
        <div className="card overflow-x-auto p-4">
          <h2 className="text-[14px] font-semibold">方案对比</h2>
          <table className="mt-2 w-full border-collapse text-[13px]">
            <thead>
              <tr className="text-left">
                {["方案", "路径", "节税效果", "风险", "成本", "建议"].map((head) => (
                  <th key={head} className="border-b py-1.5 pr-3 font-medium" style={{ borderColor: "var(--border)" }}>
                    {head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.comparison.rows.map((row, index) => (
                <tr key={index}>
                  <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    {row.name}
                  </td>
                  <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    {row.path_name}
                  </td>
                  <td className="tabular border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    {row.saving_text}
                  </td>
                  <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    <span className={`tag ${RISK_TONE[row.risk_level] ?? "tag"}`}>
                      {RISK_LABEL[row.risk_level] ?? row.risk_level}
                    </span>
                  </td>
                  <td className="muted border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    {row.cost}
                  </td>
                  <td className="border-b py-1.5 pr-3" style={{ borderColor: "var(--border)" }}>
                    {row.advice}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.comparison.combinations.length ? (
            <ul className="muted mt-3 list-disc space-y-1 pl-5 text-[12.5px]">
              {result.comparison.combinations.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {result.plans.map((plan) => (
        <PlanCard key={plan.code} plan={plan} />
      ))}

      {result.withheld.length ? (
        <div className="card p-4" style={{ borderColor: "#e3b4b6" }}>
          <h2 className="text-[14px] font-semibold text-[var(--danger)]">
            高风险方案（不发给用户，仅复核台可见）
          </h2>
          <p className="subtle mt-1 text-[12.5px]">
            这些方案属于"容易被认定为不具合理商业目的"的做法，系统不会把它们作为建议给到客户。
          </p>
          <div className="mt-3 space-y-3">
            {result.withheld.map((plan) => (
              <PlanCard key={plan.code} plan={plan} />
            ))}
          </div>
        </div>
      ) : null}

      <p className="subtle text-[12.5px] leading-6">{result.disclaimer}</p>
    </>
  );
}

function PlanCard({ plan }: { plan: PlanRecord }) {
  const [open, setOpen] = useState(false);
  const m = plan.measurement ?? {};
  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`tag ${RISK_TONE[plan.risk_level] ?? "tag"}`}>
          {RISK_LABEL[plan.risk_level] ?? plan.risk_level}
        </span>
        <span className="text-[14px] font-semibold">{plan.name}</span>
        <span className="tag">{plan.path_name}</span>
        {plan.missing.length ? <span className="tag tag-warning">待补信息</span> : null}
      </div>
      <p className="mt-1.5 text-[13px]">{plan.mechanism}</p>
      <div className="muted mt-1 text-[12.5px]">
        节税测算：
        {m.computable ? (
          <span className="tabular">
            {formatMoney(m.before)} → {formatMoney(m.after)}，节税 {formatMoney(m.saving)} 元/年
          </span>
        ) : (
          <span>无法测算——{m.note || "参数不足"}</span>
        )}
      </div>
      {plan.missing.length ? (
        <p className="mt-1 text-[12.5px] text-[var(--warning)]">缺：{plan.missing.join("；")}</p>
      ) : null}

      <button type="button" className="btn mt-2" onClick={() => setOpen((prev) => !prev)} aria-expanded={open}>
        {open ? "收起详情" : "展开详情"}
      </button>

      {open ? (
        <div className="mt-3 space-y-2 border-t pt-3 text-[13px]" style={{ borderColor: "var(--border)" }}>
          <Detail title="依据" items={plan.citations} />
          <Detail title="适用条件" items={plan.preconditions} />
          <Detail title="实施动作" items={plan.actions} />
          <Detail title="需要准备的材料 / 证据" items={plan.evidence} />
          <Detail title="滥用边界（做到这一步就违规）" items={plan.abuse_boundary} tone="danger" />
          <Detail title="被否案例" items={plan.rejected_cases} tone="warning" />
          {plan.cost ? <p className="muted">成本：{plan.cost}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

function Detail({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone?: "danger" | "warning";
}) {
  if (!items?.length) return null;
  const color = tone === "danger" ? "var(--danger)" : tone === "warning" ? "var(--warning)" : undefined;
  return (
    <div>
      <h3 className="text-[13px] font-medium" style={{ color }}>
        {title}
      </h3>
      <ul className="muted mt-0.5 list-disc space-y-0.5 pl-5">
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

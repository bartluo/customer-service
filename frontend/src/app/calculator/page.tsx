"use client";

import { useState } from "react";

import { CalculationSteps } from "@/components/answer/AnswerView";
import { ErrorState, Loading, SectionTitle } from "@/components/ui";
import { api, formatMoney } from "@/lib/api";
import type { CalcBlock, TaxCalcResponse } from "@/lib/types";

// 独立税费计算器：不提问也能用，支持增值税 + 附加税费。
//
// 两条与"算得可信"直接相关的做法：
//   1. 金额传字符串，不用浮点数字——0.1+0.2 的误差会一路进申报表；
//   2. 算不出来时（例如没选纳税人身份）明确说"为什么算不了"，
//      不默认按某一种算法给个数。
const BUSINESS_TYPES = [
  "销售货物",
  "加工修理修配劳务",
  "交通运输服务",
  "建筑服务",
  "租赁服务",
  "现代服务",
  "生活服务",
];

export default function CalculatorPage() {
  const [taxpayer, setTaxpayer] = useState("小规模纳税人");
  const [business, setBusiness] = useState("销售货物");
  const [amount, setAmount] = useState("100000");
  const [includesTax, setIncludesTax] = useState(true);
  const [location, setLocation] = useState("市区");
  // 期间口径：小规模纳税人的免税额按月 10 万 / 按季 30 万判断，
  // 用户填的是月收入还是季度收入，结论可能不同（2026-10-02 定的规则）。
  const [periodScope, setPeriodScope] = useState("month");
  const [inputVat, setInputVat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<TaxCalcResponse | null>(null);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api.calcTax({
        taxpayer_type: taxpayer,
        business_type: business,
        sales_amount: amount,
        amount_includes_tax: includesTax,
        location,
        period_scope: periodScope,
        input_vat: inputVat === "" ? null : inputVat,
        include_surcharges: true,
      });
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "计算失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[380px_minmax(0,1fr)]">
      <div>
        <SectionTitle level={1} hint="只用这一页也能算；数字全部来自计算引擎，每一步都能看到依据。">
          税费计算器
        </SectionTitle>
        <form className="card space-y-3 p-4" onSubmit={run}>
          <div>
            <label className="label" htmlFor="taxpayer">
              纳税人身份
            </label>
            <select
              id="taxpayer"
              className="field"
              value={taxpayer}
              onChange={(event) => setTaxpayer(event.target.value)}
            >
              <option value="一般纳税人">一般纳税人</option>
              <option value="小规模纳税人">小规模纳税人</option>
            </select>
          </div>

          <div>
            <label className="label" htmlFor="business">
              业务类型（用于匹配税率）
            </label>
            <select
              id="business"
              className="field"
              value={business}
              onChange={(event) => setBusiness(event.target.value)}
            >
              {BUSINESS_TYPES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="amount">
              {periodScope === "month" ? "月销售额（元）" : "季度销售额（元）"}
            </label>
            <input
              id="amount"
              className="field tabular"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
            <label className="mt-1 flex items-center gap-2 text-[12.5px]">
              <input
                type="checkbox"
                checked={includesTax}
                onChange={(event) => setIncludesTax(event.target.checked)}
              />
              这个金额是含税金额
            </label>
          </div>

          <div>
            <label className="label" htmlFor="period-scope">
              这笔收入是哪个期间的
            </label>
            <select
              id="period-scope"
              className="field"
              value={periodScope}
              onChange={(event) => setPeriodScope(event.target.value)}
            >
              <option value="month">一个月的收入（判断标准：月销售额 10 万元）</option>
              <option value="quarter">一个季度的收入（判断标准：季度销售额 30 万元）</option>
            </select>
          </div>

          <div>
            <label className="label" htmlFor="location">
              所在地（决定城建税档位）
            </label>
            <select
              id="location"
              className="field"
              value={location}
              onChange={(event) => setLocation(event.target.value)}
            >
              <option value="市区">市区（7%）</option>
              <option value="县城、镇">县城、镇（5%）</option>
              <option value="其他">其他（1%）</option>
            </select>
          </div>

          <div>
            <label className="label" htmlFor="input-vat">
              可抵扣进项税额（一般计税才需要）
            </label>
            <input
              id="input-vat"
              className="field tabular"
              inputMode="decimal"
              value={inputVat}
              placeholder="留空按 0 计算并提示"
              onChange={(event) => setInputVat(event.target.value)}
            />
          </div>

          <button type="submit" className="btn btn-primary w-full" disabled={busy || !amount}>
            {busy ? "计算中…" : "计算"}
          </button>
        </form>
      </div>

      <div className="space-y-4">
        {busy ? <Loading label="正在按规则计算…" /> : null}
        {error ? <ErrorState message={error} /> : null}
        {result ? (
          <>
            <div className="card p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-[15px] font-semibold">本期合计应纳</h2>
                <span className="tabular text-[20px] font-semibold">
                  {formatMoney(result.total_payable)} 元
                </span>
              </div>
              <p className="subtle mt-1 text-[12.5px]">规则版本 {result.rule_version}</p>
            </div>
            <Block title="增值税" block={result.vat} />
            {result.surcharges ? <Block title="附加税费" block={result.surcharges} /> : null}
            {result.notes.length ? (
              <div className="card p-3">
                <h3 className="text-[13px] font-semibold">提示</h3>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-[13px]">
                  {result.notes.map((note, index) => (
                    <li key={index}>{note}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <p className="subtle text-[12.5px] leading-6">{result.disclaimer}</p>
          </>
        ) : (
          <div className="card subtle p-6 text-center text-[13px]">
            左侧填好参数后点「计算」，这里会显示公式、代入过程与依据。
          </div>
        )}
      </div>
    </div>
  );
}

function Block({ title, block }: { title: string; block: CalcBlock }) {
  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[14px] font-semibold">
          {title}
          {block.method ? (
            <span className="muted ml-2 text-[12.5px]">
              {block.method === "general" ? "一般计税" : "简易计税"}
            </span>
          ) : null}
        </h2>
        <span className="tabular text-[15px] font-semibold">{formatMoney(block.payable)} 元</span>
      </div>
      {block.discount_applied ? (
        <p className="mt-1 text-[12.5px] text-[var(--success)]">已适用优惠：{block.discount_applied}</p>
      ) : null}
      <div className="mt-3">
        {block.computable ? (
          <CalculationSteps steps={block.steps} />
        ) : (
          <div className="rounded border px-3 py-2 text-[13px]"
               style={{ borderColor: "#e6cd9a", background: "var(--warning-soft)", color: "var(--warning)" }}>
            <p className="font-medium">这一块暂时算不出来</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {block.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

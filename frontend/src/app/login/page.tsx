"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, setToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("root_admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api.login(username, password);
      setToken(data.access_token);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="card w-full max-w-[360px] p-5">
        <h1 className="text-[17px] font-semibold">财税知识引擎</h1>
        <p className="subtle mt-1 text-[12.5px]">
          登录后可提问、算税、生成筹划方案；专家与管理员另有对应入口。
        </p>

        <div className="mt-4 space-y-3">
          <div>
            <label className="label" htmlFor="username">
              用户名
            </label>
            <input
              id="username"
              className="field"
              value={username}
              autoComplete="username"
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </div>
          <div>
            <label className="label" htmlFor="password">
              口令
            </label>
            <input
              id="password"
              type="password"
              className="field"
              value={password}
              autoComplete="current-password"
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
        </div>

        {error ? (
          <p role="alert" className="mt-3 text-[13px] text-[var(--danger)]">
            {error}
          </p>
        ) : null}

        <button type="submit" className="btn btn-primary mt-4 w-full" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>

        <p className="subtle mt-3 text-[12px] leading-5">
          固定管理员口令在首次启动时打印在后端日志里；忘了可以在项目目录执行
          <code className="mx-1 rounded bg-[var(--surface-muted)] px-1">
            docker compose exec backend python /app/scripts/reset_admin_password.py
          </code>
          重置。
        </p>
      </form>
    </div>
  );
}

"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api, clearToken, getToken } from "@/lib/api";
import type { LoginResponse } from "@/lib/types";

// 导航项。permission 为空表示"登录即可见"。
//
// 前端按权限隐藏菜单只是减少困惑，不是安全边界：真正的拦截在
// 后端每个接口的权限依赖上。前端藏起来而后端没拦，等于没拦；
// 后端拦住了而前端不藏，用户会点进去看到 403 而困惑。两边都要做，
// 但只有后端那一层算数。
const NAV: Array<{ href: string; label: string; permission: string | null }> = [
  { href: "/", label: "问答", permission: null },
  { href: "/calculator", label: "计算器", permission: "calc.run" },
  { href: "/planning", label: "筹划方案", permission: "client.profile.write" },
  { href: "/review", label: "专家复核台", permission: "planning.review" },
  { href: "/policy-changes", label: "政策提醒", permission: null },
  { href: "/admin", label: "管理后台", permission: "knowledge.review" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<LoginResponse["user"] | null>(null);
  const isLogin = pathname === "/login";

  useEffect(() => {
    if (isLogin) return;
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api
      .me()
      .then((data) => setUser(data.user))
      .catch(() => setUser(null));
  }, [isLogin, router, pathname]);

  if (isLogin) return <>{children}</>;

  const can = (permission: string | null) => {
    if (!permission) return true;
    if (!user) return false;
    return user.is_protected || user.permissions.includes(permission);
  };

  return (
    <div className="min-h-screen">
      <header className="border-b bg-white" style={{ borderColor: "var(--border)" }}>
        <div className="mx-auto flex max-w-[1200px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <Link href="/" className="whitespace-nowrap text-[15px] font-semibold">
            财税知识引擎
          </Link>
          <nav aria-label="主导航" className="flex flex-wrap items-center gap-x-1 gap-y-1">
            {NAV.filter((item) => can(item.permission)).map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className="rounded px-2.5 py-1 text-[13px]"
                  style={{
                    background: active ? "var(--accent-soft)" : "transparent",
                    color: active ? "var(--accent)" : "var(--text-muted)",
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-[13px]">
            {user ? (
              <>
                <span className="muted">
                  {user.display_name || user.username}
                  {user.is_protected ? "（固定管理员）" : ""}
                </span>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    clearToken();
                    router.replace("/login");
                  }}
                >
                  退出
                </button>
              </>
            ) : (
              <span className="muted" role="status">
                正在确认身份…
              </span>
            )}
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-[1200px] px-4 py-6">
        {children}
      </main>
    </div>
  );
}

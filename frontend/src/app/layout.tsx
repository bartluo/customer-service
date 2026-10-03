import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "财税知识引擎",
  description: "让每一次回答都有据可查：判定、依据、计算、步骤、风险、说明",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {/* 跳过导航：键盘用户不必每次 Tab 过七个菜单才能到达正文 */}
        <a className="skip-link" href="#main">
          跳到主要内容
        </a>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}

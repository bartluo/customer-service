# 前端

Next.js（App Router）+ TypeScript。界面按技术方案 7.3 的约定渲染后端返回的
**结构化答案**（按段的 `renderer` 决定画法），不解析自然语言。

## 页面

| 路由 | 内容 |
|---|---|
| `/login` | 登录（拿 24 小时令牌） |
| `/` | 问答：六段式答案、引用卡片（可点开看原文）、计算分步、免责与知识截至时间 |
| `/calculator` | 独立税费计算器（增值税 + 附加税费） |
| `/planning` | 筹划方案对比台（画像 → 方案对比表 → 详情含滥用边界） |
| `/review` | 专家复核台（一屏判定、放行/修改后放行/驳回、待转评测清单） |
| `/admin` | 管理后台（知识管理 / 缺口清单 / 评测报告 / 运行监控） |
| `/policy-changes` | 政策变化提醒（按影响程度排序） |

## 目录

```
src/app/            页面（App Router，每个目录一个路由）
src/components/     复用组件（AppShell 导航、ui 状态块、answer 答案与引用卡片）
src/lib/api.ts      接口封装（带令牌、401 自动跳登录、错误取 detail）
src/lib/types.ts    接口数据结构（与 backend/app/schemas 对应，手写以便类型检查）
```

## 本地开发

```powershell
cd frontend
npm install
npm run dev          # http://localhost:3000
```

开发态通过 `next.config.mjs` 的 `rewrites` 把 `/api` 反代到 `BACKEND_URL`
（默认 `http://127.0.0.1:8000`）。要指到别的后端就设环境变量：

```powershell
$env:BACKEND_URL = "http://127.0.0.1:8010"; npm run dev
```

## 容器部署

```powershell
docker compose up -d frontend
```

注意：`rewrites` 的目标地址会被写进**构建产物**，所以 `BACKEND_URL`
必须在 `docker build` 时确定（`docker-compose.yml` 里用 build args 传）。
只在运行时设环境变量是不生效的——这是容易踩的坑。

## 三个刻意的做法

1. **状态三件套**：每个数据区都有加载中 / 出错 / 空数据三种显示。
   空白页和"加载失败"长得一样，用户分不清是没数据还是系统坏了。
2. **权限只在前端隐藏，判定在后端**：菜单按权限过滤是为了少让用户困惑，
   真正的拦截在每个接口的权限依赖上。
3. **不做"AI 风格"**：不用紫蓝渐变、不用大圆角与重阴影；单主色 + 中性灰 + 1px 描边，
   数字用等宽对齐。财税界面要能长时间看、要能横向对齐核对。

## 已知限制

- 令牌存在 `localStorage`（XSS 可读）。对外提供服务前应改为 httpOnly Cookie。
- 没有流式输出：长答案要等整段返回（约 2～5 秒）。
- 没有前端单元测试：当前靠 `npm run typecheck` 与 `scripts/verify_api.py` 的
  端到端检查兜底。

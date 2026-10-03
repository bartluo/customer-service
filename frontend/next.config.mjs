/** Next.js 配置。
 *
 * 用 rewrites 把 /api 反代到后端，而不是让浏览器直连后端：
 *   · 浏览器只认一个域名，不用处理跨域（后端 CORS 白名单也不用来回改）；
 *   · 私有化部署时后端地址只在一处配置（BACKEND_URL），换环境不用改前端代码。
 */

const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;

# 前端镜像。多阶段构建：装依赖 → 构建 → 只带运行必需的东西。
#
# 为什么用多阶段构建：构建阶段要装几千个包和 TypeScript 编译器，
# 运行阶段只需要 next 运行时。塞在一起会让镜像从几百 MB 涨到 1GB 以上，
# 私有化交付时拷贝一次就要多等几分钟。

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json ./
# 用 npm install 而不是 npm ci：仓库里没有提交 package-lock.json。
# 有锁文件后应该换成 npm ci（可复现、更快）。
RUN npm install

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# rewrites 里的后端地址在**构建期**就被写进路由清单，所以这里必须给对；
# 只在运行时设 BACKEND_URL 是没用的（容易踩的坑）。
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=$BACKEND_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.mjs ./next.config.mjs
EXPOSE 3000
CMD ["npm", "start"]

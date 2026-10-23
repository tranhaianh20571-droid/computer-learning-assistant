# 产品前端

React / TypeScript / Vite；npm 依赖真源为 package.json 与 package-lock.json。

在本目录执行 `npm ci` 后，使用 `npm run dev` 启动、`npm run build` 检查类型并构建、`npm run lint` 检查代码。默认开发端口 5173，API 代理目标在 vite.config.ts 配置。

`src/views/` 为已有页面目录；`src/ui/` 为 UI 组件，`hooks/` 为交互钩子，`lib/` 为板书、流与其他纯逻辑。本次不移动业务源文件，不改变 import。

离线设计在[文档 UI](../文档/ui/README.md)，浏览器专项在根 tests/。前端产物不由后端镜像构建或托管。

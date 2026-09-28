# 白屏问题修复说明

## 问题描述
访问 `index.html#/settings/models` 或 `index.html#/settings/account` 时出现白屏，内容无法显示。

## 根本原因
设置页面的HTML结构与CSS布局不匹配：

### 原始问题
1. `.app` 使用 `display: grid; grid-template-columns: var(--side) minmax(0,1fr)` 定义了**两列布局**（侧边栏 + 主区域）
2. 设置页面的 `shell` 函数返回的结构只有**顶栏 + settings-layout**，没有侧边栏
3. Grid布局将这两个元素错误地分配到两列，导致内容溢出或被隐藏

### 原始HTML结构（错误）
```html
<div class="app">  <!-- 期望2列grid，但实际只有2个子元素且不符合预期 -->
  <header class="top">...</header>
  <div class="settings-layout">...</div>
</div>
```

## 修复方案

### 1. CSS修改（第214-215行）
添加 `.app.settings-page` 类，覆盖默认的grid布局：
```css
.app.settings-page { display: flex; flex-direction: column; }
```

### 2. JavaScript修改（第1282-1286行）
在 `shell` 函数中，为设置页面添加 `settings-page` 类：
```javascript
if (isSettingsPage) {
  return '<div class="app settings-page"><header class="top">...</header>' + body + '</div>';
}
```

### 修复后的HTML结构（正确）
```html
<div class="app settings-page">  <!-- flex容器，垂直排列 -->
  <header class="top">...</header>  <!-- 固定高度56px -->
  <div class="settings-layout">  <!-- flex:1，占据剩余空间 -->
    <nav class="settings-nav">...</nav>
    <div class="settings-content">...</div>
  </div>
</div>
```

## 布局说明

### 正常页面（有侧边栏）
```
.app (grid 2列)
├── .side (侧边栏, 236px)
└── .main (主区域)
    ├── .top (顶栏, 56px)
    └── .page (内容区)
```

### 设置页面（无侧边栏）
```
.app.settings-page (flex 列)
├── .top (顶栏, 56px)
└── .settings-layout (flex:1, grid 2列)
    ├── .settings-nav (设置导航, 240px)
    └── .settings-content (设置内容)
        └── .page (实际内容)
```

## 测试
1. 直接访问：打开 `index.html#/settings/models`
2. 使用测试页面：打开 `test.html` 查看多个页面的对比效果
3. 验证其他路由未受影响：`#/home`, `#/course`, `#/lecture` 等

## 影响范围
- ✓ 修复了 `/settings/models` 白屏问题
- ✓ 修复了 `/settings/account` 白屏问题  
- ✓ 保持了其他所有页面的正常布局
- ✓ 响应式布局（窄屏）仍然有效

## 文件变更
- `index.html` 第214-215行（CSS）
- `index.html` 第1286行（JavaScript shell函数）

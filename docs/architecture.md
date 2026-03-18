# 架构说明

## 目标

- 让 CLI 入口保持轻量
- 把运行时编排、依赖构造、状态持久化、命令执行分层
- 避免后续功能继续堆回 `src/discorsair/cli.py`

## 当前分层

### 1. CLI 入口

- 文件：`src/discorsair/cli.py`
- 职责：
  - 解析命令行参数
  - 兼容 `--config` 在子命令后面的写法
  - 处理 `init`
  - 调用 runtime 并渲染 JSON 输出

### 2. Runtime 生命周期

- 文件：`src/discorsair/runtime/runner.py`
- 职责：
  - 创建 runtime
  - 管理服务生命周期
  - 统一异常边界
  - 调用命令 handler

### 3. Runtime 结构化配置

- 文件：`src/discorsair/runtime/settings.py`
- 职责：
  - 从 `app_config` 派生结构化设置对象
  - 收口 watch / store / server 相关运行参数

### 4. Runtime 依赖构造

- 文件：`src/discorsair/runtime/factory.py`
- 职责：
  - 加载配置并初始化日志
  - 构造 `store` / `client` / `queue` / `notifier`
  - 生成 `RuntimeServices`

### 5. Runtime 状态持久化

- 文件：`src/discorsair/runtime/state.py`
- 职责：
  - 写回账号状态
  - 在成功请求后保存非空且有变化的 cookie
  - 落盘配置文件

### 6. Runtime 命令执行

- 目录：`src/discorsair/runtime/commands/`
- 职责：
  - `status.py`：状态查询
  - `notify.py`：通知测试
  - `actions.py`：`daily` / `like` / `reply`
  - `watch.py`：`run` / `watch`
  - `serve.py`：HTTP 控制服务
  - `context.py`：命令执行上下文

## 依赖方向

推荐保持下面这个方向，不要反向依赖：

```text
cli.py
  -> runtime/runner.py
    -> runtime/commands/*
    -> runtime/factory.py
    -> runtime/state.py
    -> runtime/settings.py
```

约束：

- `cli.py` 不直接操作 `store` / `client` / `notifier`
- `runtime/commands/*` 不直接写配置文件
- `runtime/state.py` 不负责具体命令分发
- `runtime/factory.py` 不负责业务流程

## 兼容层

- `src/discorsair/runtime/__init__.py`

`src/discorsair/runtime/__init__.py` 当前主要用于公开导出常用 runtime 类型，避免上层调用方依赖过深的内部路径。

## 导入约定

### 公开入口

适用场景：

- CLI 或其他上层调用方
- 需要少量稳定 runtime 类型时

推荐：

- `discorsair.runtime`

例如：

```python
from discorsair.runtime import DiscorsairRuntime
```

### 内部实现

适用场景：

- `runtime/` 子系统内部模块之间
- 测试需要针对具体实现层打桩或断言

推荐：

- 直接使用 `discorsair.runtime.*`

例如：

```python
from discorsair.runtime.runner import DiscorsairRuntime
from discorsair.runtime.commands import handle_authenticated_command
from discorsair.runtime.state import RuntimeStateStore
```

## 兼容策略

- 不再新增新的兼容转发文件
- 旧兼容层在内部引用全部切换完成后，优先删除而不是继续保留
- 只有 `src/discorsair/runtime/__init__.py` 作为 runtime 子系统的公开 facade 保留

## 后续建议

- 新增 runtime 相关逻辑时，优先放入 `src/discorsair/runtime/`
- 新命令优先作为 `src/discorsair/runtime/commands/` 下的新模块
- 如果某层再次变胖，继续按职责拆分，不回退到顶层大文件

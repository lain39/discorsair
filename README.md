# Discorsair

Discourse 自动巡帖与数据采集工具。

## 运行方式

CLI 命令名：`discorsair`

## CLI

- 常用命令：`run` / `watch` / `daily` / `like` / `reply` / `export` / `import` / `status` / `notify test` / `init` / `serve`
- `run` 和 `watch` 当前共用同一套 watch 循环实现与参数
- `status` / `daily` / `like` / `reply` / `export` / `import` / `notify test` 默认输出 JSON，便于脚本处理
- 详细命令说明、参数与输出示例见 `docs/cli.md`
- `status` 输出也包含插件状态快照：后端类型、已启用插件、运行态计数，以及插件持久态摘要（今日计数、once 标记数量、KV key 列表）
- 插件开发说明见 `docs/plugin-development.md`

## 配置

- 主配置：`config/app.json`
- 运行时状态：与配置文件同目录同名的 `*.state.json`，例如 `config/app.json -> config/app.state.json`
- 账号配置：`config/app.json` 内的 `auth`
- 模板参考：`config/app.json.template`
- 必填：`site.base_url`
- `auth.cookie` 需要在 `app.json`、对应的 `*.state.json` 或环境变量里至少提供一处
- 敏感字段支持环境变量覆盖：`DISCORSAIR_AUTH_COOKIE`、`DISCORSAIR_AUTH_NAME`、`DISCORSAIR_AUTH_KEY`、`DISCORSAIR_NOTIFY_URL`、`DISCORSAIR_POSTGRES_DSN`
- 存储后端：`storage.backend`（`sqlite` / `postgres`，默认 `sqlite`）
- SQLite 路径：`storage.path`（默认 `data/discorsair.db`）
- SQLite 按站点分库：`storage.auto_per_site`
- PostgreSQL DSN：`storage.postgres.dsn`
- 站点爬虫锁目录：`storage.lock_dir`
- 爬取模式下同一 `site` 同一时刻只允许一个进程运行；会基于 `storage.lock_dir` 创建按站点区分的 crawl lock
- 旧 SQLite schema 不提供迁移脚本；如果命中 schema mismatch，直接删除旧库后重建
- 抓取：`crawl.enabled`（是否抓取帖子内容）
- 调试：`debug`（更详细日志）
- 日志文件：`logging.path`
- 请求节流：`request.min_interval_secs`（默认 1 秒）
- 未读优先：`watch.use_unseen`
- 队列：`queue.maxsize`
- 通知：`notify.enabled` + `notify.url` + `notify.chat_id`
- 通知自动已读：`notify.auto_mark_read`（默认关闭；当当前未读通知都已在本地去重状态中时，调用 mark-read 全部标记为已读）
- 插件：`plugins.dir` + `plugins.items`（示例插件见 `plugins/sample_forum_ops/`；其中回复/点赞代码默认注释）
- 通知前缀：`notify.prefix` / `notify.error_prefix`
- 服务：`discorsair serve` 启动 HTTP 控制服务
- 控制接口超时：`server.action_timeout_secs`（`0` 表示不设超时）
- 服务默认仅监听 `127.0.0.1`
- 如果 `server.host` 或 `--host` 使用非回环地址，必须配置 `server.api_key`
- `server.schedule` / `server.interval_secs` / `server.max_posts_per_interval` 仅作用于 `serve` 模式下的 watch 线程；`run/watch` 仍以 CLI 参数为准
- 启动时按 `app.json -> *.state.json -> 环境变量` 的顺序合并 `auth` 状态
- 运行时只会写回 `*.state.json` 里的 `auth` 状态，不再修改 `app.json`
- `*.state.json` 不会在启动时预先生成；首次发生受管 `auth` 状态写入时才会自动生成
- 如果要手工修复运行时状态，直接修改对应的 `*.state.json`，或者删除它后等待后续运行时状态重新写入
- `serve` 模式下如果遇到登录失效或 unresolved challenge，会停止 watch、关闭 HTTP 服务，并以非 0 退出

## PostgreSQL

- 先安装可选依赖：`uv sync --extra postgres`
- 也可以不把 DSN 写进配置文件，改用环境变量：`DISCORSAIR_POSTGRES_DSN`
- `storage.backend` 设为 `postgres` 后，运行时使用 `storage.postgres.dsn` 连接数据库；`storage.path` / `storage.auto_per_site` 会被忽略
- PostgreSQL 模式是“单库多站点、多账号共存”；SQLite 仍是按站点分文件
- 只需要先手动建库，不需要手动建表；首次启动时会自动初始化 schema
- 爬取模式下的 crawl lock 仍按 `site` 生效，和 SQLite 一样继续使用 `storage.lock_dir`
- `discorsair status` / HTTP `GET /watch/status` 在 PostgreSQL 下返回的 `storage_path` 是脱敏后的 DSN，不是文件路径

配置示例：

```jsonc
"storage": {
  "backend": "postgres",
  "path": "data/discorsair.db", // postgres 模式下忽略
  "auto_per_site": true,        // postgres 模式下忽略
  "lock_dir": "data/locks",
  "postgres": {
    "dsn": "postgresql://user:password@127.0.0.1:5432/discorsair"
  }
}
```

典型流程：

- 新建数据库后，直接执行 `discorsair run` / `watch` / `serve`，程序会自动建表
- SQLite 导出：`discorsair --config config/sqlite.json export --output ./export`
- 导入 PostgreSQL：`discorsair --config config/postgres.json import --input ./export`
- 同样也支持 PostgreSQL -> SQLite、PostgreSQL -> PostgreSQL 的导出/导入

## 开发

- 安装开发依赖：`uv sync --group dev`
- 本地检查总入口：`make check`
- 单独跑静态检查：`make lint`、`make static`
- 单独跑测试：`make test`
- 构建与包元数据校验：`make build`
- 发布前约束校验：`make release-check TAG=v0.1.0`
- CI 会执行：release guard、`ruff check`、`compileall`、单元测试、构建、`twine check`
- `plugins/` 默认忽略本地自用插件，只保留 `plugins/sample_forum_ops/` 示例插件；如果你要提交自用插件，需要手动 `git add -f`
- 如果要使用 PostgreSQL 后端，先安装可选依赖：`uv sync --extra postgres`
- PostgreSQL 集成测试入口：`DISCORSAIR_PG_TEST_DSN=postgresql://... uv run --extra postgres python -m unittest tests.test_postgres_integration`
- CI 也会单独跑一条 PostgreSQL 集成测试 job
- 数据迁移命令：`discorsair export --output ./export`、`discorsair import --input ./export`
- Schema 规划与表结构见 `docs/schema.md`

## 结构

- `config/` 配置
- `src/` 源码
- `docs/` 文档
- `tests/` 测试
- 架构说明：`docs/architecture.md`

## 备注

- Cookie 建议新建一个隐私窗口来获取，获取后关闭窗口，以免会话冲突导致 cookie 失效。首次导入时建议只保留 `_t`。

# Discorsair

Discourse 自动巡帖工具。

## 使用边界

- 项目默认面向“本人账号、本人数据、本人控制环境”的个人使用场景，主要用途是保持账号活跃度。
- 不建议用于商用采集、攻击性压测、权限绕过等其他高风险用途。
- FlareSolverr、代理、Cookie、数据库、插件和外部通知服务均由使用者自行配置和承担风险。

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
- HTTP 控制接口返回 `504` 只表示调用方等待超时，不保证动作未生效；底层请求可能仍已发出或完成
- 服务默认仅监听 `127.0.0.1`
- 如果 `server.host` 或 `--host` 使用非回环地址，必须配置 `server.api_key`
- `server.schedule` / `server.interval_secs` / `server.max_posts_per_interval` 仅作用于 `serve` 模式下的 watch 线程；`run/watch` 仍以 CLI 参数为准
- 启动时按 `app.json -> *.state.json -> 环境变量` 的顺序合并 `auth` 状态
- 运行时只会写回 `*.state.json` 里的 `auth` 状态，不再修改 `app.json`
- `*.state.json` 不会在启动时预先生成；首次发生受管 `auth` 状态写入时才会自动生成
- 运行时写回的 `_t` 必须已经被一次成功交互实际带到服务器；如果响应里刚拿到更新的 `_t`，会先留在内存，等后续成功交互验证后再写回
- 如果要手工修复运行时状态，直接修改对应的 `*.state.json`，或者删除它后等待后续运行时状态重新写入
- `serve` 模式下如果遇到登录失效或 unresolved challenge，会停止 watch 并把 watch 标记为 blocked；HTTP 服务继续存活
- watch 被 `auth_invalid` / `unresolved_challenge` 阻塞后，可用 HTTP `POST /auth/cookie` 更新 `_t`，再用 `POST /watch/start` 恢复，或直接 `POST /watch/start {"force": true}` 强制重试
- `POST /auth/cookie` / `force=true` 的设计目标是“同一账号刷新登录态”，不是“跨账号热切换”；如果要换号，建议重启进程并使用目标账号配置重新启动
- HTTP `GET /` 和 `GET /healthz` 都是公开的轻量状态端点，不受 `server.api_key` 保护；返回 `{"ok":true}`，适合做容器保活和外部探活
- `queue.maxsize` 只限制 ready/running 的请求；已进入 `429` 冷却等待的 delayed 请求不受这个上限约束

## 容器部署

- 仓库内提供了一个从官方 FlareSolverr 镜像出发的单容器方案：`Dockerfile` + `docker-entrypoint.sh`
- 容器内会同时启动：
  - FlareSolverr：`127.0.0.1:8191`
  - Discorsair `serve`：`0.0.0.0:17880`
- 只暴露 Discorsair 的 `17880`；FlareSolverr 只供容器内访问
- 镜像默认不打包你本地的 `config/*.json` / `*.state.json`；需要在运行时挂载或自行派生镜像提供 `config/app.json`
- 镜像默认使用 SQLite，并把数据目录约定为 `/data`

推荐的容器配置：

```jsonc
{
  "storage": {
    "backend": "sqlite",
    "path": "/data/discorsair.db",
    "lock_dir": "/data/locks"
  },
  "flaresolverr": {
    "base_url": "http://127.0.0.1:8191",
    "in_docker": false
  },
  "server": {
    "host": "0.0.0.0",
    "port": 17880,
    "api_key": ""
  }
}
```

建议把敏感值放环境变量：

- `DISCORSAIR_AUTH_COOKIE`
- `DISCORSAIR_AUTH_KEY`
- 可选：`DISCORSAIR_CONFIG`
- 可选：`DISCORSAIR_SERVER_HOST`
- 可选：`DISCORSAIR_SERVER_PORT`
- 可选：`FLARESOLVERR_INTERNAL_URL`
- 可选：`FLARESOLVERR_STARTUP_TIMEOUT_SECS`

最省事的本地启动方式是直接使用仓库内的 `docker-compose.yml`：

```bash
DISCORSAIR_AUTH_COOKIE='_t=...' \
DISCORSAIR_AUTH_KEY='replace-me' \
docker compose up --build
```

也可以先把它们写进仓库根目录的 `.env` 再执行 `docker compose up --build`；仓库提供了可提交的 `.env.example`，而本地 `.env` 已被 `.gitignore` 忽略。

它会：

- 构建当前仓库镜像
- 挂载 `./config/app.json` 到容器内
- 挂载命名卷 `discorsair-data` 到 `/data`
- 暴露 `17880`

本地构建：

```bash
docker build -t discorsair-flaresolverr .
```

运行示例：

```bash
docker run --rm \
  -p 17880:17880 \
  -v "$(pwd)/config/app.json:/app/config/app.json:ro" \
  -v discorsair-data:/data \
  -e DISCORSAIR_AUTH_COOKIE='_t=...' \
  -e DISCORSAIR_AUTH_KEY='replace-me' \
  discorsair-flaresolverr
```

说明：

- 如果你传了容器命令参数，入口脚本会先启动 FlareSolverr
- 当参数看起来像 `watch` / `serve` / `run` 这类 Discorsair 子命令时，会自动执行 `discorsair --config <config-path> ...`
- 如果你传的是完整外部命令，比如 `bash`，则按原样执行，不自动补 `discorsair`
- 如果不挂载 `/data`，SQLite、lock 目录和运行时状态会随容器销毁一起丢失
- 如果你想把配置直接烘进镜像，可以基于当前 `Dockerfile` 再写一层派生镜像，把你自己的 `config/app.json` 复制进去
- `FLARESOLVERR_INTERNAL_URL` 用于告诉入口脚本去哪里探测容器内 FlareSolverr 的就绪状态，默认是 `http://127.0.0.1:8191`
- `FLARESOLVERR_STARTUP_TIMEOUT_SECS` 控制入口脚本等待 FlareSolverr 启动完成的超时时间，默认 `60`

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
- 当前导入导出实现会按表整批读入内存，更适合中小规模数据，不适合超大库直接整库搬迁

## 开发

- 安装开发依赖：`uv sync --group dev`
- 本地检查总入口：`make check`
- 单独跑静态检查：`make lint`、`make static`
- 单独跑测试：`make test`
- 构建与包元数据校验：`make build`
- 发布前约束校验：`make release-check TAG=v0.1.2`
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

## 许可证

本项目采用 MIT License。

## 免责声明

- 本项目主要用于学习、研究和个人使用。
- 使用者需自行确认目标站点服务条款、robots 规则及所在地区法律法规，并自行承担使用风险。
- 作者不对账号受限、限流、封禁、数据缺失、第三方服务异常或由此产生的直接/间接损失负责。

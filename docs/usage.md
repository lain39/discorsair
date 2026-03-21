# 使用说明（草案）

## 启动

- 命令：`discorsair`

## 配置

- `config/app.json` 写入站点与请求相关配置
- `config/app.json` 写入站点与账号配置
- `config/app.json.template` 作为模板参考
- `site.base_url` 站点根地址（必填）
- `site.timeout_secs` 单次请求超时（秒）
- `auth.cookie` 登录 cookie（必填），配置中建议只保存 `_t=...`
- `auth.proxy` 可设置代理（可留空）
- `auth.name` 账号标识（用于通知前缀）
- 支持用环境变量覆盖敏感字段：`DISCORSAIR_AUTH_COOKIE -> auth.cookie`、`DISCORSAIR_AUTH_NAME -> auth.name`、`DISCORSAIR_AUTH_KEY -> server.api_key`、`DISCORSAIR_NOTIFY_URL -> notify.url`
- 环境变量覆盖发生在读取配置文件之后、校验之前；因此 `auth.cookie` 可以不写在文件里，改由 `DISCORSAIR_AUTH_COOKIE` 注入
- `auth.disabled=true` 时会阻止当前账号启动
- `auth.status` / `auth.disabled` / `auth.last_ok` / `auth.last_fail` / `auth.last_error` / `auth.note` 主要用于运行时状态记录
- `request.user_agent` 为空时，优先使用 `impersonate_target` 对应的内置 UA；若没有映射且启用了 FlareSolverr，则会通过 `ua_probe_url`（默认 `data:,`）获取
- `request.impersonate_target` 指定 `curl_cffi` impersonate 目标；缺省或显式留空都会按空值处理，运行时再依赖内置 UA 映射、UA 探测或后续推断来决定
- UA 探测只用于获取 `userAgent`，不会携带当前站点 cookie，也不会把 probe 返回的 cookie 写回账号状态
- `request.max_retries` 请求失败后的额外重试次数；`0` 表示无限重试。默认值为 `1`，与旧版默认实际行为一致
- 如果 Discourse 返回 `429` 且运行时能解析出 `Retry-After` 或响应体里的 `wait_seconds/time_left`，会按该等待时间再额外加 5 秒缓冲，进行接口级等待；当前这次调用不会丢弃，而是延后后重试
- 上述可解析等待时间的 `429` 不会让整个请求队列睡死：其他独立接口仍可继续执行；但发起这次调用的那条业务链会停在当前接口上等待结果，不会跳过它继续往后跑
- 对当前 `watch` 流程来说，`get_topic` / `post_timings` 被 `429` 时，当前 topic 会停在这里等待；正常情况下不会在同一条 watch 链里无限积压更多同类请求
- 遇到 `BAD CSRF` 时会强制重新请求 `/session/csrf` 获取新 token，不会仅复用当前内存里的缓存 token
- `flaresolverr.ua_probe_url` 可省略，不填时默认使用 `data:,`
- `storage.path` 指定 SQLite 存储路径（默认 `data/discorsair.db`）
- `storage.auto_per_site` 按站点自动区分数据库文件
- `storage.rotate_daily` 按天分库（文件名追加日期）
- `crawl.enabled` 控制是否启用帖子内容抓取（默认 `true`）
- `debug` 启用详细日志（请求地址、请求头、响应等，敏感字段会脱敏）
- `logging.path` 日志文件路径（空表示不写文件）
- `request.min_interval_secs` 每次请求的最小间隔（默认 `1` 秒）
- `watch.use_unseen` 优先使用 `/unseen.json`（空则回退到 `/latest.json`）
- `watch.timings_per_topic` 每次刷多少楼层（默认 30）
- `queue.maxsize` 请求队列长度（0 为无限）
- `queue.maxsize` 只限制 ready/running 的任务；已进入 `429` 冷却等待的 delayed 任务不会占满这个容量
- `notify.enabled` 启用通知
- `notify.interval_secs` 通知轮询间隔（默认 600 秒）
- `notify.url` 通知接口地址（类似 Telegram `sendMessage`）
- `notify.chat_id` 通知目标
- `notify.prefix` 消息前缀（默认 `[Discorsair]`）
- `notify.error_prefix` 错误消息前缀（默认 `[Discorsair][error]`）
- `notify.headers` 通知请求头
- `notify.timeout_secs` 通知请求超时
- `server.host` / `server.port` 监听地址
- 默认仅监听 `127.0.0.1`
- `server.action_timeout_secs` HTTP 控制接口（如 `/like`、`/reply`）的等待超时；超时返回 `504`，不影响 watch；`0` 表示不设超时
- `server.interval_secs` watch 轮询间隔
- `server.max_posts_per_interval` 每轮最多抓取帖子数
- `server.schedule` 运行时段（如 `08:00-12:00`）
- `server.auto_restart` watch 线程自动重启
- `server.restart_backoff_secs` 自动重启间隔
- `server.max_restarts` 最大重启次数（0 为不限制）
- `server.same_error_stop_threshold` 连续相同错误次数达到阈值后自动停止（0 为关闭）
- `server.api_key` HTTP 服务鉴权（为空则不启用）
- 当 `server.host` 或 `discorsair serve --host` 使用非回环地址时，必须配置 `server.api_key`
- `serve` 模式下，如果 watch 线程或 HTTP 控制接口命中登录失效 / unresolved challenge，会停止 watch、关闭 HTTP 服务，并以非 0 退出
- 其中登录失效会把账号标记为 `invalid` 并禁用；其他 fatal 错误会写入 `auth.last_fail` / `auth.last_error`
- `time.timezone` 时区（用于今日统计与运行时段）
- 模板文件为 JSONC（允许 `//` 注释），程序也支持读取 JSONC

## FlareSolverr

- `flaresolverr.enabled=false` 时禁用 FlareSolverr 兜底
- `flaresolverr.base_url` FlareSolverr 服务地址
- `flaresolverr.request_timeout_secs` FlareSolverr 请求超时
- `flaresolverr.use_base_url_for_csrf=true` 时，获取 CSRF 会改为用 FlareSolverr 访问 `base_url` 并提取页面里的 `<meta name="csrf-token" ...>`；关闭时仍使用 `/session/csrf`
- 该路径仍会沿用运行时的 UA 对齐、请求串行化、限流与重试/backoff 逻辑
- 如果 FlareSolverr 返回了 cookie 但页面里没有可提取的 `csrf-token`，会按登录失效处理
- `flaresolverr.in_docker=true` 表示 FlareSolverr 运行在 Docker 中；为 `false` 时，传给 FlareSolverr 的代理不会把 `127.0.0.1/localhost` 替换为 `host.docker.internal`
- 需提前部署 FlareSolverr；如果运行在 Docker 中，`flaresolverr.in_docker` 应保持为 `true`
- 如果 `auth.proxy` 使用回环地址（如 `http://127.0.0.1:7890`）且 `flaresolverr.in_docker=true`，传给 FlareSolverr 的代理会转换为 `http://host.docker.internal:7890`
- 如果 `auth.proxy` 包含认证信息，配置里应保持 URL 编码形式；`curl_cffi` 直接使用该 URL，FlareSolverr 会改为 `{"url","username","password"}` 结构并对账号密码做 URL 解码后再发送
- 该转换由 `src/core/` 处理
- 过盾时也会使用 FlareSolverr 访问 `base_url`；如果返回 HTML 含 `<meta name="csrf-token" ...>`，运行时会提取该 token，并用于本次重试及后续请求的 CSRF 同步
- `cf_clearance` 可按代理 IP 做本地缓存，下次同 IP 先尝试复用
- 如果过盾后重试仍然命中 `challenge still present after solve`，运行时会清理当前站点 cookie，只保留 `_t`，并丢弃当前代理的 `cf_clearance` 缓存，再按现有重试策略继续
- 运行时仅在成功请求后才会把最新 `_t` 写回 `auth.cookie`；其他 cookie 不会持久化到配置，空 `_t` 或未变化的值也不会覆盖配置

## CLI

- 参见 `docs/cli.md`
- 架构参见 `docs/architecture.md`
- `run` 和 `watch` 当前共用同一套 watch 循环实现与参数
- `status` / `daily` / `like` / `reply` / `notify test` 默认输出 JSON
- `run` / `watch` / `serve` 主要通过日志反映运行状态

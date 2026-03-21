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
- `auth.disabled=true` 时会阻止当前账号启动
- `auth.status` / `auth.disabled` / `auth.last_ok` / `auth.last_fail` / `auth.last_error` / `auth.note` 主要用于运行时状态记录
- `request.user_agent` 为空时，优先使用 `impersonate_target` 对应的内置 UA；若没有映射且启用了 FlareSolverr，则会通过 `ua_probe_url`（默认 `data:,`）获取
- `request.impersonate_target` 指定 `curl_cffi` impersonate 目标；留空时使用默认值
- UA 探测只用于获取 `userAgent`，不会携带当前站点 cookie，也不会把 probe 返回的 cookie 写回账号状态
- `request.max_retries` 请求失败后的额外重试次数；`0` 表示无限重试。默认值为 `1`，与旧版默认实际行为一致
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
- `queue.timeout_secs` 队列任务超时时间
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
- `server.interval_secs` watch 轮询间隔
- `server.max_posts_per_interval` 每轮最多抓取帖子数
- `server.schedule` 运行时段（如 `08:00-12:00`）
- `server.auto_restart` watch 线程自动重启
- `server.restart_backoff_secs` 自动重启间隔
- `server.max_restarts` 最大重启次数（0 为不限制）
- `server.same_error_stop_threshold` 连续相同错误次数达到阈值后自动停止（0 为关闭）
- `server.api_key` HTTP 服务鉴权（为空则不启用）
- 当 `server.host` 或 `discorsair serve --host` 使用非回环地址时，必须配置 `server.api_key`
- `time.timezone` 时区（用于今日统计与运行时段）
- 模板文件为 JSONC（允许 `//` 注释），程序也支持读取 JSONC

## FlareSolverr

- `flaresolverr.enabled=false` 时禁用 FlareSolverr 兜底
- `flaresolverr.base_url` FlareSolverr 服务地址
- `flaresolverr.request_timeout_secs` FlareSolverr 请求超时
- 需提前在 Docker 中部署 FlareSolverr
- 如果 `auth.proxy` 使用回环地址（如 `http://127.0.0.1:7890`），传给 FlareSolverr 的代理需要转换为 `http://host.docker.internal:7890`
- 如果 `auth.proxy` 包含认证信息，配置里应保持 URL 编码形式；`curl_cffi` 直接使用该 URL，FlareSolverr 会改为 `{"url","username","password"}` 结构并对账号密码做 URL 解码后再发送
- 该转换由 `src/core/` 处理
- 过盾时会使用 FlareSolverr 访问 `base_url`；如果返回 HTML 含 `<meta name="csrf-token" ...>`，运行时会提取该 token，并用于本次重试及后续请求的 CSRF 同步
- `cf_clearance` 可按代理 IP 做本地缓存，下次同 IP 先尝试复用
- 运行时仅在成功请求后才会把最新 `_t` 写回 `auth.cookie`；其他 cookie 不会持久化到配置，空 `_t` 或未变化的值也不会覆盖配置

## CLI

- 参见 `docs/cli.md`
- 架构参见 `docs/architecture.md`
- `run` 和 `watch` 当前共用同一套 watch 循环实现与参数
- `status` / `daily` / `like` / `reply` / `notify test` 默认输出 JSON
- `run` / `watch` / `serve` 主要通过日志反映运行状态

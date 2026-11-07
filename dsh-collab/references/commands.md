# dsh-collab 命令参考

所有子命令输出**一个 JSON 对象**到 stdout；进度与诊断走 stderr。

## observe：只读取新增进展

```powershell
& "<PY>" "<SCRIPT>" observe --task <ID>
& "<PY>" "<SCRIPT>" observe --task <ID> --after <next_cursor> --wait 30
```

支持同步/异步及历史后端任务，不连接 DSH。默认立即返回；wait 必须在 0..30 秒。首次从当前轮次开始，输出 `events`、`next_cursor`、`has_more`、`partial_line`、`gap`、`no_new_events`、`execution_complete`、`evidence` 和原日志路径。`status` 仍来自 task.json；`evidence=needs_verification/changed_during_read` 时不得宣称已完成。

默认最多 20 条摘要、约 16 KiB 摘要正文；普通事件合并，关键事件单列，按原日志顺序显示。每条包含 `offset/end_offset`（UTF-8 文件字节范围），截断摘要保留原日志定位。达到限制时下一事件保留，不跳过；重复游标是只读重放。未完成末行不消费，完整损坏记录报告 gap 且不跨过。游标绑定 task 和日志文件，身份改变、截断或无效边界报错；不要手动构造游标。

观察不会 ack、approve、取消或重试。无新事件不表示卡死；审批摘要只证明曾发生请求。原始工具参数/输出和私有推理不返回，普通可见文本只计数，final 返回脱敏截断片段。运行中不能核验终态时诚实保留未确认。

## review 的证据门禁

`review --approve` 仅允许 awaiting_review/needs_decision，要求当前 session/round/run 对应的最终回复及成功 run_end，且在 task 锁内校验。旧日志缺证据时可查询和追加意见，不放宽批准条件。被拒绝不会修改批准记录或任务；普通 review 不改变执行状态。

needs_decision 的问题解决由 Codex 实质核查并记录，代码不使用关键词或非空文本自动判断解决。代码校验不替代独立验收；失败任务的人工接手可记录意见，但没有强制批准旁路。

## MCP：可选 stdio 封装

入口 `scripts/dsh_mcp.py`，使用本机 Python MCP SDK 2.x（实测 2.2.0），无 HTTP 监听。支持进程启动参数 `--state-root`、`--desktop-endpoint`、`--electron-exe`；不把这些路径作为每次模型工具输入。

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| dsh_start | workspace、message；可选 title、timeout=1800、max_rounds=12 | 既有 async submit，先检查 Desktop，再创建新任务 |
| dsh_followup | task、workspace、message；可选 recover=false、timeout=1800 | 同 task/session 的 async submit；已有 checkpoint 要求显式 recover=true |
| dsh_status | task | 既有 show；存在 async.json 时返回既有 async status |
| dsh_observe | task；可选 after、wait=0 | 与 CLI observe 完全相同 |

message 仅经 UTF-8 临时文件传给命令，适配层完成后清理临时输入；现有任务日志仍按原规则保存派单内容。子进程禁用 shell。返回原接受状态和退出码，错误通过 MCP isError 返回；starting 不改为 accepted。适配层命令超时报告 acceptance=unknown，不自动重试，检查原任务避免重复副作用。

现有异步 checkpoint 的成功续接也需 recover=true；先核对 worker/monitor 停止和副作用。MCP 不新增批准或模型切换工具。只读观察不确认 pending 通知。CLI 继续独立可用。

本机注册项为 `mcp_servers.dsh_collab`，command 指向 Python，args 指向安装后的脚本。调用超时配置 240 秒以容纳既有提交握手；observe 自身最多等待 30 秒。配置写入、客户端加载、真实 Desktop 连接是三个独立检查，不能互相替代。

全局参数（必须写在子命令**前面**）：

| 参数 | 作用 |
| --- | --- |
| `--state-root <目录>` | 覆盖任务日志根（默认 `$CODEX_HOME\dsh-collab\tasks` 或 `~\.codex\dsh-collab\tasks`） |
| `--dsh-cmd <绝对路径>` | headless CLI 路径（优先于 `$DSH_CMD`） |
| `--desktop-endpoint <绝对路径>` | Desktop 连接描述文件（优先于 `$DSH_DESKTOP_ENDPOINT`） |
| `--electron-exe <绝对路径>` | 运行 Desktop 传输助手的 Electron 可执行文件（优先于 `$DSH_DESKTOP_EXECUTABLE`） |

后端选择（`start` / `prepare` / `models` / `select-model` / `send` 各自的 `--backend`）：

1. 显式 `--backend desktop|headless`
2. 环境变量 `$DSH_COLLAB_BACKEND`；或 `$DSH_COLLAB_HEADLESS=1` 表示 headless
3. 已有任务记录里的 `backend`（历史任务没有该字段 → `headless`）
4. 默认 `desktop`

对**已存在**的任务，请求的后端和它记录的后端不一致会直接被拒绝（不会中途换后端）；
失败后**永远不会**自动降级到另一个后端——Desktop 缺失或失败**不是**回退到 headless 的
授权，只有用户明确要求才显式用 `--backend headless`。

---

## doctor

```powershell
dsh_collab.py doctor [--require-desktop] [--no-desktop-probe]
```

检查 Python / DSH 可执行文件 / 状态目录可写性 / Desktop 描述文件与连接。
**默认用法就是带 `--require-desktop`**（Desktop 是默认后端，不可达即失败）。

- 不带 `--require-desktop` 时 Desktop 状态只是**信息性**的（没有 Desktop 也能拿到可用报告）。
- `--require-desktop`：Desktop 不可达时以失败退出（`ok: false`）。
- `--no-desktop-probe`：只读描述文件，不真的连 Desktop。
- **不会**打印认证 URL、token、cookie；只报告 host/port/pid/默认模型。

返回里 `backends.desktop` 含：`endpoint_file`、`present`、`protocol`、`backend`、`pid`、
`pid_alive`、`host`、`port`、`has_launch_token`、`reachable`、`default_model`、`providers`。

## start

```powershell
dsh_collab.py start --workspace <绝对路径> --message-file <文件> [--title T]
                    [--timeout 1800] [--max-rounds 12] [--backend desktop|headless]
```

新建任务 + 新 session，跑第一轮（自动注入 bridge brief）。
Desktop 后端下会在同一次调用里注册 workspace、创建会话、命名，然后订阅并发送。

## prepare（仅 Desktop）

```powershell
dsh_collab.py prepare --workspace <绝对路径> [--title T] [--timeout 1800] [--max-rounds 12]
```

只创建**空会话**（注册 workspace + `session/create` + `session/rename`），**不调用任何
模型**，不订阅事件。任务状态为 `prepared`，`rounds` 为 0。之后的第一次 `send` 会带上
bridge brief。

用于已授权的自动推理强度策略需调整首轮档位，或用户指定模型/强度且相应全局默认副作用已获授权时，在 `prepare` 之后
`select-model`，再 `send`。目标档位与默认一致时直接 `start`；自动策略见 SKILL.md「自动选择推理强度（已授权）」，不逐次询问。

两点如实说明：

- 没有跑过任何一轮的会话在 **Desktop 侧栏里是隐藏的**（只有 `turn/start` 之后才显示），
  所以**不能**把 `prepare` 说成"用户先在 Desktop 里看到会话/挑模型"；模型只能用
  `select-model` 指定，`--task` 是唯一的句柄。
- 返回里的 `model_selection` 会写明模型没被动过；`next` 指向第一轮 `send`。

## send

```powershell
dsh_collab.py send --task <TASK_ID> --workspace <绝对路径> --message-file <文件>
                   [--timeout 1800] [--recover] [--backend ...]
```

在任务的**同一个** session 里继续一轮。`--backend` 必须和任务记录一致。
**不会**调用 `selectModel`，用户选好的模型保持不动。

## models

```powershell
dsh_collab.py models
```

打印 Desktop 的实时模型目录（只读，不推理）：`default`、`providers`、`groups`
（每个模型含 `reasoning.efforts` 的 id/name）。

## select-model（仅 Desktop）

```powershell
dsh_collab.py select-model --task <TASK_ID> [--workspace <绝对路径>]
                           --provider <P> --model <M> [--reasoning-effort <E>]
```

对任务的 Desktop 会话设置模型。会先取实时目录校验 provider/model/reasoning effort，
非法选项按**用法错误**（退出码 2）拒绝，不会调用 `selectModel`。

成功只写进 `task.json` 的 **`selected_model`**（请求值）与 conversation 日志，**绝不**
改 `actual_model`：`actual_model` 只由每一轮 Desktop 的 header 事件写入，代表那一轮
**实际**跑的是什么。所以"选了 flash/max 但实际跑 pro/low，之后再选回 flash/max"之后，
`actual_model` 仍然是 pro/low，不会被抹掉。

注意：`selectModel` 是 Desktop 的原生 API，**这次选择也会被保存为 Desktop 的默认模型**
（原生行为），不只是这一个会话。执行前说明并取得该副作用的授权；指定会话模型不自动授权改变全局默认。用户已于 2026-11-06 授权按难度自动调节推理强度及其全局默认强度副作用，执行该策略无需重复确认；仍须保持当前 provider/model，遵守 Skill 的会话/全局模型一致性及并发检查。更换模型不在此授权内。无相应授权时不调用，沿用默认或由用户在 Desktop 选择。

## review / show / list

```powershell
dsh_collab.py review --task <TASK_ID> --workspace <绝对路径> --message-file <证据> [--approve]
dsh_collab.py show   --task <TASK_ID>
dsh_collab.py list   [--workspace <绝对路径>]
```

`review` 只记录证据/结论，**永不调用 DSH**；只有显式 `--approve` 才算批准。
`list` 每行含 `backend`、`workspace_id`、`session_id`、`selected_model`、`actual_model`。

---

## 一次 Desktop 轮的内部行为

1. 读取连接描述文件（协议 1 / backend desktop / 回环 http + 单个 token），
   **拒绝**非回环、非 http、带额外查询参数的描述文件。
2. 用主机自己的启动 token 换取主机自己的普通浏览器会话 cookie
   （`GET /?token=...` → 303 + Set-Cookie）。不读别人的 cookie、不伪造、不关闭权限、
   不新增未鉴权路由、不跟随重定向、不走代理。
3. 需要时 `workspace/create` → `session/create` → `session/rename`。
4. `session/list` 检查会话是否**正在运行**；忙会话直接拒绝并提示到 Desktop 处理。
5. 打开 `/api/remote.mux` WebSocket，`session/follow` 拿 snapshot。
6. `session/prompt`（`mode: queue`，带 `requestId` 与 `clientTimeZone`）。
7. 只用**自己 requestId** 对应的 `user/message` 定位本轮，用该轮的 `turn/start`/`turn/end`
   判定完成；旧历史、别人的消息、别的回合的 `turn/end` 都**不算数**。
8. `assistant/message` 里只取可见 `text` 块：reasoning/thinking 正文一律不进回复与日志。
9. 只有 `completed` 且已拿到与本轮 `turn/end` 关联的文本，才算这一轮成功；`max-tokens`、
   `aborted`、其他 reason、缺少本轮 `turn/end`/final/文本，一律算失败。
10. 服务端正常结束 follow 流（`end`）时：若本轮已 `completed` 且有文本，按成功收尾（这不是传输
    错误）；若本轮还没有 `turn/end`，按"流在回合结束前结束"失败处理——**绝不**把缺少本轮
    `completed turn/end` 的断连当成功。

超时：向 Desktop 请求 `session/cancel` 并报告结果；**不杀 Desktop 进程**。措辞只在 helper
回 `stopped: true` 时才说"已停止"；否则一律写"已请求取消、停止未确认，任务可能仍在运行"。
取消帧的写入**不等 stdin 锁**（写任务正文的线程可能长时间持锁）：超过 5s 拿不到锁就放弃，
并如实报告"取消请求未送达、状态未确认"，不让超时路径挂死。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 命令成功（`ok: true`） |
| 1 | 任务/执行失败（含 `dsh_failed`） |
| 2 | 用法或输入错误（含非法的后端、非法的模型选项） |

## 轮次交接与恢复

派单与结果格式见 [紧凑协作约定](collaboration.md)，按需读取；只传任务特有信息，不复制通用规则。同步/异步的选择见 [Skill](../SKILL.md#选择同步或异步)。下面只说明命令恢复语义。

DSH 用现有独立行 DSH_RESULT / REVIEW_READY / QUESTION 标记，不创建新控制标记。QUESTION 仅用于真实阻塞；异议或非阻塞建议用正文，无问题时不写 `QUESTION: 无`。Codex 结合桥接返回的 task_id/session_id/rounds/status 和 task.json 续接，不要求 DSH 猜 task 元数据。prepared 不表示运行过；awaiting_review 不表示批准；needs_decision 先由 Codex 处理常规决定。running 可能是残留状态，不能单凭它断定进程仍活跃。

### 失败分类与恢复前检查

| 阶段 | Codex 的检查与决定 |
| --- | --- |
| doctor 启动检查失败 | 不派单；报告命令/退出码/原因；可能尚无 task ID，不能虚构日志或会话 |
| 建任务/会话或启动失败 | 保留已生成的 task/session 与日志，确认是否已发送，再决定诊断、停止或显式恢复 |
| 传输失败/超时/中断 | 结合 last_error、run_end、取消结果、Desktop 会话/进程、工作区 diff 判断；无回复不代表无副作用 |
| 部分修改后失败 | 读取真实改动及测试证据，先确认停止或协调写入，再决定原任务续接或明确接手剩余工作 |
| 本轮成功但证据不足 | 不 approve；在原任务补实验、要求修正或停止，不伪装执行失败或自报验收通过 |

恢复前用 show 与 conversation.md/events.jsonl 检查用户明确指定任务、workspace、backend、session、rounds、last_error、actual_model；独立检查产物和活跃执行。历史任务无 backend 按 headless 处理，不能切换。Desktop 恢复派单前再次 doctor --require-desktop。

首次投递失败且无法确认 brief 已进入会话时，在恢复正文中显式补齐当前目标、授权/验收标准及首轮协作契约（可读取 scripts/dsh_collab.py 的 FIRST_TURN_BRIEF，替换 workspace 后合入消息文件）。桥接按已尝试轮数判断首轮，不会为 --recover 自动重发 brief；不要重放可能已执行的操作。若证据无法判断先前副作用，先诊断或停止，不盲目续发。

**没有 recover 子命令**；恢复是现有 send 的 `--recover`：

```powershell
dsh_collab.py send --task <原TASK_ID> --workspace <原绝对路径> --message-file <后续消息文件> --recover
```

dsh_failed/interrupted 或残留 running 需要显式 --recover；该参数不是停止仍活跃任务、免除副作用检查或自动重试的许可。若失败发生在建立 session 之前，send --recover 会拒绝，不能给旧 task 强行附加新 session；Codex 检查现场后可明确决定另开新任务，并在派单及旧任务日志中互相引用、说明已发生的副作用与剩余范围。这是有记录的新决策，不是自动重试。approved 不能继续 send；达到 max-rounds 不通过另建任务绕过。停止或预算耗尽时在现有日志用 review 记录实际结论，不加 --approve，不引入新 status。

失败交接包含可用的 task/session、status、backend、selected_model/actual_model、失败阶段、命令/退出码、日志、潜在副作用与下一步建议。review 只记录真实独立检查证据或未通过原因，不执行验收，也不修改 DSH 模型。

### 当前能力边界

start/send 是同步等待；prepare/start/send/show/list/review 与显式 --recover 支持持久任务和同 session 续接。原 Codex 聊天自动恢复仍未实现（Phase A 结论 B：宿主无已验证的恢复/注入接口），不以后台 Python 监听器冒充宿主唤醒能力。

可选的**异步执行与客观监控**由同目录的 `dsh_async.py` 提供（标准库，同步命令不变），详见下节。

---

## 异步执行与客观监控（dsh_async.py，MVP）

同步 CLI 保持不变；异步是**同一套** `task.json` / `events.jsonl` / 锁 / `run_turn` 之上的薄封装。

```powershell
# 全局参数（写在子命令前）：--state-root / --desktop-endpoint / --electron-exe
dsh_async.py submit  --workspace <绝对路径> --message-file <文件> [--title T]
                     [--task 原TASK_ID --recover] [--timeout 1800] [--max-rounds 12]
                     [--backend desktop] [--origin-codex-thread-id <id>]
                     [--handshake-timeout 25] [--monitor-interval 5]
                     [--monitor-max-interval 60] [--post-terminal-wait 30]
dsh_async.py status  --task <TASK_ID>
dsh_async.py pending [--task <TASK_ID>] [--workspace <绝对路径>]
dsh_async.py ack     --task <TASK_ID> --event <EVENT_ID> [--actor codex] [--note 文本]
dsh_async.py watch   --task <TASK_ID> [--once] [--interval 5] [--max-interval 60]
```

### submit

- 只接受 **Desktop**：`--backend headless`、历史 headless 任务一律用法错误（退出码 2），不静默回退。
- 提交前跑与 `doctor --require-desktop` 相同的检查；失败不派单、不创建假运行状态。
- 新建任务：生成 task_id，写 `task.json` 后复制消息到受控输入文件 `input/round-N.message`（记录 sha256），写不可变 checkpoint `async.json`，再启动两个**独立分离进程**（Windows `CREATE_NO_WINDOW + DETACHED_PROCESS`，stdin DEVNULL，stdout/stderr 写文件）：
  - worker：立即取得**原 task.lock**，再写 `worker.json` 握手，运行现有 `run_turn`（完整 Desktop 传输，不提前断开 follow）；
  - monitor：写 `monitor.json` 心跳并进入低频退避循环。
- 事务：每个 task 一把**独立 submit 锁**串行化"检查→归档→写 checkpoint→起 worker→握手"；并发 submit 直接失败，不会重写消息或旧 checkpoint。
- 返回条件：worker 握手（其 sidecar 必须与本 job/task/workspace/session 绑定）**且**执行锁确实被 worker 持有**且** monitor 心跳就绪。**不等整轮完成**。`status` 只有观察到真实 `prompt_accepted` 才是 `accepted`，否则是 `starting`（`turn_started:false`），**绝不把 prepared/starting 说成 DSH 已运行**；短任务已完成时直接返回终态并标记 `completed_before_return`。握手失败如实返回并保留仍活跃的执行事实，不覆盖、不停止。
- worker 在**执行锁内**重新校验 job_id、预期轮数 `round-1` 与任务状态；新任务接受 `new/prepared`，续接沿用原任务恢复、approved/max-rounds 门禁（见「跨轮通知与身份」），不要求把 awaiting_review / needs_decision 改成 new；旧 checkpoint 的 worker 直接拒绝执行。
- 已有 task 若正在执行、存在存活 async worker/monitor、monitor 锁仍被占用、或没有显式 `--recover`，拒绝重派；归档旧 checkpoint 前确认旧 worker 已死且旧 monitor 已退出。不自动重派单、不自动重试有副作用任务。

### status / pending / ack

- `status`：读取 `task.json`（执行真相，校验 schema=1）、`async.json`（checkpoint）、`worker.json`/`monitor.json`（活跃事实，校验 job/task/workspace/session 绑定）、任务 `events.jsonl`（run_end 证据）与独立 `async-events.jsonl`（通知/ack 账本）；返回 `health`、pending 数、pending 事件、acks、诊断、`incomplete` 与 `manual_recovery`。文件损坏/绑定不符/schema 不符时 `ok:false` + 明确诊断，不假成功。
- `pending`：只读、幂等，列出未 ack 的客观事件；任务级扫描时跳过没有 async checkpoint 的同步任务。
- `ack`：只写一条 append-only 消费记录（`event_ack`），**不 approve、不改 task 状态、不触发执行**；重复 ack 幂等；未知/别的任务的 event_id 拒绝（退出码 2）。

### watch（可重启 monitor）

- 单实例：`watch.lock`（OS 文件锁）保证同一任务只有一个 monitor 在写账本；第二个实例明确失败。
- 只做客观判断：只认**本 job 预期轮**的 `run_end`，并同时校验 (a) task.json 当前 status 已是匹配终态、(b) round 与 job 预期轮一致、(c) 成功/决策两侧 session 均已建立且相等、(d) run_end 的 run id 与 worker 自己记录的 run id 一致、(e) worker sidecar 绑定同一 job/task/workspace；全部满足才产生唯一终态事件。
- task.json 尚未 save 的短暂竞态：只报 `in_progress`，给足 grace；worker 已退出仍不一致 → `state_inconsistent`。旧轮 run_end 一律忽略。
- 终态未出现时：worker 仍活 → 继续低频退避观察；worker 消失/身份不符 → 只报 `task_unknown`（stalled/unknown），**不标完成/取消，不自动重跑**；`worker.json=finished` 却没有 verified run_end → `state_inconsistent`；task.json/events.jsonl 损坏或 schema 不符 → `state_unreadable` + 诊断。
- `watch` 返回 `outcome` 与 `ok`：terminal/running 且无诊断为 `ok:true`；unknown/inconsistent/unreadable/incomplete 为 `ok:false` + `incomplete:true`。`--once` 可重复运行。
- 通知写独立 `async-events.jsonl`（append-only、`events-append.lock` check-and-append 去重）；截断尾部先补换行再追加。不写 run_turn 的 `events.jsonl`。
- 事件去重：`event_id` 由 task/round/run/type/session 决定性生成；重启 monitor 不会重复写。
- worker 与 monitor 完全解耦：monitor 退出/被结束后 worker 继续跑；`watch --once` 可从磁盘重新推导同样事实。
- 不持有执行锁观察长轮；不修改 task 执行状态机。

### 事件字段（append-only，`source: "async"`）

`ts`、`source`、`event_type`、`event_id`、`dedup_key`、`task_id`、`workspace`、`session_id`、`round`、`run`、`task_status`（快照）、`actual_model`（缺失为 `unknown`）、`selected_model`、`backend`、`logs`、`job_id`、`origin`（含原 Codex thread id 与 recovery=manual_unavailable）、`worker`（pid/state）、`side_effects`（rounds/max_rounds/error/classification…）、`wake`（adapter/status/delivered/error）。

### 唤醒与恢复（如实边界）

- 默认 wake adapter 固定为 `manual/unavailable`：**没有**调用任何未验证的 Codex 接口，也不会启动新会话冒充原会话。
- intent-first：通知先持久化（含 manual/unavailable 事实）才可能尝试投递；本阶段默认**不调用**任何 adapter。仅测试/实验注入 adapter 时在持久化后调用一次，结果写入独立 `wake_delivery`/`wake_delivery_failed` 记录；失败/不确定不自动重发，通知仍 pending 可查。
- 每个通知在 `pending`/`ack` 前都校验 task/job/workspace/session/round 绑定；跨任务 event_id 一律拒绝。
- 正确恢复：Codex 用 `status`/`pending` 读取事件与日志，检查工作区与副作用，再用同步 `send --task <原ID> --recover` 续接原 session；`ack` 只是消费记录。
- 任务完成事件不等于验收通过；`review --approve` 仍是唯一批准动作。

### 退出码

与同步 CLI 一致：0 成功、1 任务/读取失败、2 用法/输入错误（含 headless 异步、缺 session 恢复、跨任务 ack）。

### 跨轮通知与身份

同任务的新异步轮次在旧 worker 和 monitor 退出后显式 `submit --task <原ID> --recover ...`。worker 取得原执行锁后重新检查预期轮数、恢复和 approved/max-rounds 门禁。成功的 awaiting_review / needs_decision 任务可继续，不要求伪造 new 状态；失败/残留 running 必须显式恢复且先检查副作用。

旧 checkpoint 用唯一文件名归档，通知/ack 账本保留。status/pending 验证当前及归档 checkpoint，旧轮未消费通知仍可查看并确认，不误归入当前 job。worker/monitor 的 OS process_creation_epoch 以小精度误差匹配（0.02秒）；started_epoch 不作身份证据。通知只存安全事实和证据路径，不复制模型异议或问题全文。

自动恢复原 Codex 聊天不可用；CLI 的 resume / app-server schema 与可读历史 thread 不表示 Desktop 原主机已连接。MVP 不启动新的 Codex 模型会话，不注册系统自启动，不在监控器里判断验收质量。主机重启后由用户或宿主显式执行 watch 重建客观事实；不会重跑执行任务。

---
name: dsh-collab
description: 在 Codex 已决定委派 DSH 后使用，提供 Desktop 执行、多轮证据反馈、会话续接、失败恢复与独立评审记录。普通问答、只读检查、简单修改通常由 Codex 本地完成；本技能不要求所有任务调用 DSH。
---

# dsh-collab：DSH 执行桥接（可选执行端点）

DSH 是**可选执行端点**：是否使用由 Codex 按任务性质、成本、风险、环境决定；Codex 也可以自己完成小改动，或独自做独立验证。当选择把执行交给 DSH 时，**DSH 完成 ≠ 验收通过**，Codex 必须独立验收（见「评审纪律」）。

- 本技能只讲**桥接执行**；任务类型、复杂度、HANDOFF、DECISION REPORT 等通用协议在全局 `AGENTS.md`，本技能不复制。
- 即使技能发现目录尚未刷新，也可以按绝对路径直接读取本文件。

## 路由前置条件与职责

先由 Codex 比较任务性质、风险、环境、上下文与委派成本，再决定 LOCAL 或 DELEGATE_DSH。普通问答、只读检查、简单文件修改、小型 Bug 和配置调整通常本地完成；复杂、批量、长时、重复或独立执行收益明显的工作才考虑 DSH。研究按实验与验证负担判断，不以文件数、行数或 token 阈值机械分流。用户指定执行端优先，权限与环境仍须检查。

职责沿用全局 AGENTS；派单只补本任务信息。Codex 在首次委派前明确可观察的验收条件，DSH 自主实施、验证并整合证据；Codex 按风险独立验收。实际模型以 Desktop 事件为准，不将 DSH 与 DeepSeek 模型等同。

## 默认走 Desktop

新任务默认交给**正在运行的 Desktop（DeepSeek Harness 应用）**执行：任务、用户消息、回复都出现在 Desktop 会话里。**用户没指定模型时沿用 Desktop 默认模型，推理强度按下述已授权的自动策略选择**，不要为了选模型每个任务都停下来问用户。

- 用之前请确认 **Desktop 保持打开**；关掉 Desktop 时任务会**明确失败**，**这不是**改用 headless CLI 的授权。
- 只有**用户明确要求**时才显式加 `--backend headless`（例如"用命令行跑""不要开桌面窗口"）。Desktop 缺失或失败本身**不构成**擅自回退的理由；如实报告失败即可。
- 历史任务（`task.json` 里没有 `backend` 字段，或记录的 backend 与请求不一致）**仍绑定原 backend**：不能中途换后端，也不会因为 Desktop 出问题就改跑 headless。
- 任务会话挂在**对应项目**下面：用户点侧栏里的会话标题即可查看。本桥接**不承诺**自动切换/聚焦窗口。

## 入口命令（装好后用完整路径）

```
<PYTHON> <CODEX_HOME>\skills\dsh-collab\scripts\dsh_collab.py
```

全局参数写在子命令**前面**：`--state-root`、`--dsh-cmd`、`--desktop-endpoint`、`--electron-exe`。

```powershell
& "<PYTHON>" "<CODEX_HOME>\skills\dsh-collab\scripts\dsh_collab.py" doctor --require-desktop
```

每次 Desktop 派单前跑 `doctor --require-desktop`；它检查 Python / DSH / Desktop 连接与默认模型，**不推理、不打印任何凭据**。历史 headless 任务仍绑定原后端，不因该检查而切换。

## 模型与成本

派单前用 doctor 的 `default_model` / `providers` 检查实际配置，目录不足时用只读 `models`。用户未指定模型时保留 Desktop 默认；若默认已是可用的用户配置 DeepSeek，就沿用。若不是，报告实际模型，不自动切换；优先经济执行的意图不授权改变 Desktop 全局默认、账号或偏好。当前 `select-model` 同时修改全局默认，调用前须向用户说明该副作用并取得授权；仅指定会话模型不自动涵盖该副作用。

请求值记录在 `selected_model`，实际值来自 `actual_model`；默认路径可能没有显式请求值，在派单摘要记录“沿用 Desktop 默认”及探测值即可，不改 schema。实际值缺失时写未知；与预期不符时如实报告，由 Codex 决定下一步，不自动重跑。续接默认保留会话模型/思考配置与原后端（异步 submit 同样保留，不自动 select-model）；仅当新证据表明难度变化且用户未明确固定强度时，按下述授权单独调用 select-model 调整强度，send 本身不改选择。利用已有轮数、时间戳、backend/model 评估成本，不虚构 token 或费用。

## 自动选择推理强度（已授权）

用户于 2026-11-06 授权启用：Codex 可按任务难度自动选择推理强度，接受 select-model 同时更新 Desktop 全局默认推理强度，不逐次询问。本授权不允许自主更换 provider/model、账号或其他偏好；前述全局默认变更须授权的要求，对本项已有授权，不重复申请。用户当场指定模型/强度或在 Desktop 手动设置时优先，不因默认策略覆盖；明确要求恢复自动选择后再恢复。

- 从只读 models 的当前 provider/model 与 reasoning.efforts 实时目录选合法档位，不假定必有 medium/high/max。明确步骤、批量重复和长时构建/测试通常选低档；常规实现/局部调试选中档；根因不明、跨模块分析、复杂实验选高档；最高档仅用于难题且有收益证据。按实际语义匹配可用档位，不按列表索引猜强弱；无法判断则保留原强度并说明。不支持调节时沿用默认，不为调强度换模型。长时间执行本身不是升档理由。
- 新任务先 doctor --require-desktop 与 models；确定目标档位。目标与默认相同可直接 start，摘要记录选择依据；不同时用 prepare → select-model（原 provider/model + 目标 effort）→ send。不新增路由命令或在 Python 中猜复杂度。
- 续接默认保留原会话配置；仅当新证据表明任务难度变化且用户未明确固定强度时，在空闲的同一 session 调整。取会话当前配置，结合 actual_model（仅代表最近一轮）与用户最新修改核对，不把当下全局默认当旧会话配置；无法确认 provider/model 时不调，先只读诊断。
- select-model 会连带保存 provider/model 为全局默认：只有会话的 provider/model 与当时 Desktop 默认一致时才自动调强度。两者不同则保留会话配置并说明，避免借强度授权改掉全局默认模型。并发任务协调选择与提交；不要用“修改后恢复默认”规避副作用，避免覆盖用户或另一任务的新设置。选择前重新核对配置，出现并发变更或选择失败时停止调整并按既有失败决策流程处理，不盲目重试。
- 每次记录选档依据、请求 provider/model/effort、已有授权及全局默认副作用；由本轮 header 核对实际强度，缺失标未知、不符如实报告。不为验证强度自动重跑已产生副作用的任务。

## 怎么传消息（PowerShell 引号陷阱）

任务正文**只走 stdin**，绝不进命令行。用 UTF-8 消息文件传：

```powershell
$msg = Join-Path $env:TEMP ("dsh-msg-" + [guid]::NewGuid().ToString("N") + ".txt")
@'
帮我把 src/app.py 里的 TODO 全部实现，并跑 pytest。
'@ | Set-Content -LiteralPath $msg -Encoding utf8
```

## 最短流程

新建任务并立刻跑第一轮（目标推理强度与 Desktop 默认一致时）：

```powershell
& "<PY>" "<SCRIPT>" start --workspace "<项目绝对路径>" --message-file $msg --title "实现 TODO"
```

自动策略需要调整首轮推理强度（已有授权），或用户明确指定模型/强度且相关全局默认副作用已获授权时，走 prepare / select-model / send：

```powershell
& "<PY>" "<SCRIPT>" prepare --workspace "<项目绝对路径>" --title "实现 TODO"   # 只建空会话，不推理
& "<PY>" "<SCRIPT>" select-model --task <TASK_ID> --provider <P> --model <M> --reasoning-effort <E>
& "<PY>" "<SCRIPT>" send --task <TASK_ID> --workspace "<项目绝对路径>" --message-file $msg
```

- `prepare` **不调用任何模型**，只注册 workspace + 建会话 + 命名。**不要**把它说成"用户可以先在 Desktop 里看到/挑模型"：没有跑过任何一轮的会话在 Desktop 侧栏里**是隐藏的**（只有 `turn/start` 之后才显示），所以模型必须用 `select-model` 指定，不能靠用户手动点。
- `select-model` 调用原生 API 时，**也会把这个选择保存为 Desktop 的默认模型**（原生 `selectModel` 的行为就是保存默认），不只是改这一个会话。
- 新任务**总是新建独立 session**，并注册/关联到该 workspace。
- `send` 的第一轮（`prepare` 之后那次）会自动带上 bridge brief。
- **`send` 不会调用 selectModel**：已经跑起来的会话，用户在 Desktop 里改的模型/思考程度后续轮次保留，不会被覆盖。要改模型只有两条路：用户在 Desktop 里改，或显式跑 `select-model`（见 references）。
- 续接同一任务必须显式给 `--task`；workspace 必须和任务绑定的一致。
- 已 `approved` 的任务拒绝 send；超过 `--max-rounds` 会在启动前被拒绝。
- 上一轮失败/被中断、或残留 `running` 状态时必须显式 `--recover`（副作用可能已经发生）。
- 首次投递失败且无法确认 brief 已进入会话时，恢复正文显式补齐当前目标、授权/验收与首轮契约；桥接按尝试轮数判断首轮，恢复不会自动补发。先检查副作用，具体方式见命令参考。

## 派单与结果检查

- 先确定验收条件再派单：从用户目标提取必要的可观察结果、兼容要求与风险，可用 A1、A2 编号；不把实现方案或填模板要求变成验收条件。
- 需要派单/结果格式时读取 [紧凑协作约定](references/collaboration.md)。只传目标、绝对工作区、必要边界、验收条件和 DSH 无法自行取得的背景；首轮 bridge brief 已提供通用职责、安全及控制标记，不再重复。缺失且无法取得的关键约束仍须给出。
- DSH 自主决定内部规划、方案、工具、子 agent 数量与并行组织、测试及修复；不新增内部配额、固定步骤或强制过程报告。Standard 通常只在开始与结果验收介入；Project / Infrastructure 仅按真实依赖和风险设关键里程碑，重大异常或权限边界再介入。桥接按轮交换，不承诺执行中实时推送。
- 验收不通过时只反馈未满足的条件编号、客观证据和必要边界，让 DSH 自主决定修复；补充轮次针对具体缺陷或证据缺口。通过后结束，不 approve 未核验结果。
- 用户明确预算与现有 timeout/max-rounds、失败/恢复门禁保持生效；它们是外层运行保护，不限制 DSH 内部工作轮次或子 agent。故障、保护到限、不可接受风险或缺少授权时返回 Codex 决策，不新增阶段预算、不新建任务绕门禁。

## 选择同步或异步

同步 start/send 保持为兼容基线。短轮、需立即方向决策或用户要求本聊天持续跟进时优先同步；可独立长时执行的 Standard 或 Project 阶段优先考虑显式异步，确认接受后可交后台运行并说明手动续接入口。用户要求跟进至完成时继续跟进与验收，不能仅提交后结束任务。

## 同步等待与显式续接（兼容基线）

### 增量观察（优先用于长任务跟进）

使用同步入口 `dsh_collab.py observe --task <ID> [--after <next_cursor>] [--wait <0..30>]`，同步和异步任务均可查询。首次从当前轮次开始，后续传回 `next_cursor`；`has_more` 时继续读取，不能丢弃游标或跳到文件末尾。返回最多 20 条精简摘要、约 16 KiB，包含动作结果、异常、审批请求、实际模型和轮次结束；普通事件合并显示，不调用模型总结。

游标只消费完整且已处理的事件；半行留待下次，损坏完整记录返回 gap，身份变化或截断报错。摘要提供原日志与字节范围，超长正文标注截断。`no_new_events` 不表示任务卡死；`execution_complete` 仅在当前任务/轮次的完成证据一致时为真，仍不等于独立验收通过。审批摘要表示曾观察到请求，实时是否仍待批以 Desktop 为准。

观察不改任务状态、不确认通知；`pending/ack` 继续负责持久通知。只有具体缺陷、gap 或证据不一致时再读取完整日志。用户要求持续跟进时在当前聊天继续观察和验收，仅在有意义变化、完成、失败或需用户处理时汇报。原聊天结束后的自动唤醒仍不可用。

### 可选 MCP 调用入口

`scripts/dsh_mcp.py` 使用已安装的 Python MCP SDK 2.x（验证版本 2.2.0），仅 stdio，由客户端按需启动。CLI 不依赖 MCP SDK。提供 `dsh_start`、`dsh_followup`、`dsh_status`、`dsh_observe` 四个工具；不提供审批、验收批准或模型切换入口。

派单与续接复用 `dsh_async.py submit`，保留原 worker/monitor、task/session 和 Desktop 检查；不存在第二套执行状态。`starting` 不代表接受；命令错误/超时后检查现场，不重发 prompt。已有异步 checkpoint 的续接即使上一轮成功也要求显式 `recover=true`：先核对副作用及旧 worker/monitor 已退出，再续接同 task/session。历史 headless 任务仍使用原 CLI，不经 MCP 改后端。

新 MCP 配置写入不等于当前聊天已经加载；未出现工具时保留 CLI 工作方式，等待调用方重新加载。MCP 工具已加载也不表示真实 Desktop 可用。

当前 start/send **同步等待**本轮结果；task.json 是唯一执行状态来源，conversation.md 与 events.jsonl 提供可读交接和事件证据。工具长调用可让出等待并在有新信息时报告进度，无新信息不高频轮询；这不等于释放后由 DSH 自动恢复已结束的 Codex 聊天。

## 可选异步执行与客观监控

使用同目录 `scripts/dsh_async.py`；首次提交或处理异步事件/恢复前，读 [命令参考的异步部分](references/commands.md)，其中保留完整参数、绑定校验、去重、锁和进程身份规则。只支持 Desktop，不改同步命令。

```powershell
& "<PY>" "<ASYNC>" submit --workspace "<项目绝对路径>" --message-file $msg --title "长任务"
& "<PY>" "<ASYNC>" status --task <TASK_ID>
& "<PY>" "<ASYNC>" pending --task <TASK_ID>
& "<PY>" "<ASYNC>" ack --task <TASK_ID> --event <EVENT_ID>
```

- 派单前仍须 doctor 与模型/强度检查；异步层不调用 select-model。需要调整时沿用 prepare → select-model → submit --task 的已授权流程。
- **可释放条件**：已确认本轮真实 `prompt_accepted`（submit 返回 `accepted` / `turn_started=true`）且 monitor 就绪，或本轮终态已验证。`starting`、`prepared`、残留 `running` 或单独 `ok:true` 均不足；starting 时低频核查接受证据，失败返回 Codex 决策，不重复 submit。
- worker 持有执行锁；monitor 只产生客观事件。`task.json` 是执行状态来源；checkpoint、sidecar 与通知须按 task/job/workspace/session/round/run 绑定，OS 进程创建时间核对身份。终态证据不全、worker 消失或状态不一致时报告 unknown/inconsistent，不宣称完成，不自动重跑。
- pending/ack 只处理持久通知，ack 不批准任务。只有 Codex 独立验收后使用同步 `review --approve` 批准。
- 续接显式原 task/session；先确认旧 worker/monitor 退出、检查副作用，再用 `submit --task <原ID> --workspace <原路径> --message-file <文件> --recover` 或同步 send。旧 checkpoint 与未消费通知保留；不自动重试或换后端。
- **原 Codex 聊天自动唤醒未实现**，wake 为 `manual/unavailable`；不自动创建新模型会话。monitor 不会系统自启动；主机重启后显式 `watch --task <ID>` 重建客观事实，不重跑执行任务。无新信息不高频轮询。

## 失败返回 Codex 决策

启动检查失败不派单；启动、传输、超时或中断失败都不自动重试、不静默回退 headless、不自动接管。报告 task/session（未创建则明说）、状态、后端、请求/实际模型、失败阶段、精确命令、退出码、日志路径、潜在副作用及恢复建议。未收到结果不表示未执行。

Codex 先核对 task.json、日志、运行进程/会话和实际工作区 diff，再明确决定诊断后恢复原任务、自行接手剩余工作、请求必要用户决策或停止。自行接手须在已有授权内，记录理由并告知用户，确认先前执行已停止或协调好共享写入，避免重复副作用。恢复使用原 task/session 与显式 --recover，不新建替代任务掩盖失败；详见命令参考。常规恢复决策不重复索取已有授权。

## 超时与中断

- 超时**不会杀死 Desktop 进程**：桥接会向 Desktop 请求 `session/cancel`，并把取消结果写进日志。措辞只在 helper 明确回 `stopped: true` 时才说"已停止"；否则一律写成"已请求取消，停止未确认，任务可能仍在运行"，此时请到 Desktop 确认。
- 如果连取消都送不出去（例如写入任务正文的线程一直占着 stdin 锁），超时路径**不会**无限等锁：到点就放弃，并把"取消请求根本没送达、状态未确认"如实写进错误里。
- 本轮完成证据不全时连接断开必须报失败；已收到本轮 `completed` 和文本后的正常结束不推翻结果。**不会**自动重复派单。

## 状态含义

| status | 含义 |
| --- | --- |
| `new` | 已建任务，还没跑过 |
| `prepared` | Desktop 空会话已建好（侧栏里暂时看不到），等第一轮 send |
| `running` | 该轮已提交/正在执行，残留状态不能证明仍活跃，恢复前检查现场 |
| `awaiting_review` | 该轮完成（收到 final），等评审 |
| `needs_decision` | DSH 抛了 `QUESTION:`，等 Codex 决策 |
| `dsh_failed` | 非零退出 / 传输中断 / 没有 final / 没有可用 session → 需要 `--recover` |
| `interrupted` | 用户中断 → 需要 `--recover`（超时记为 `dsh_failed`） |
| `approved` | 评审已批准；不能再 send |

任务记录里会写清 `backend`、`workspace_id`、`session_id`，以及两个**分开**的模型字段：`selected_model`（调用方请求的 provider/model/思考程度）和 `actual_model`（最近一轮**实际**用的，只来自 Desktop 的 header 事件）。`select-model` 只写前者，**永远不会**抹掉后者；所以"flash/max → 实际 pro/low → 再选回 flash/max"之后，`actual_model` 仍是 pro/low，直到下一轮真的跑出新的 header。

## 看日志

每个任务一个目录（默认 `$CODEX_HOME\dsh-collab\tasks\<TASK_ID>`，没设 `CODEX_HOME` 时是 `~\.codex\dsh-collab\tasks\<TASK_ID>`）：`task.json`、`conversation.md`、`events.jsonl`。`events.jsonl` **不包含**模型隐藏推理（thinking/reasoning 只留类型与计数，正文被抹掉）。

## 详细命令

实际需要命令参数或恢复操作时，只读取 [`references/commands.md`](references/commands.md) 的相关段落。task.json 提供权威执行状态；日志、事件与 diff 按当前验收条件读取，未变化内容不重复加载。

## 标准执行报告（轻量）

DSH 按实际情况使用**独立行**标记：完成用 `DSH_RESULT:`，可供评审用 `REVIEW_READY:`，存在待 Codex 处理的真实阻塞才用 `QUESTION:`。没有问题时**省略整个 QUESTION 行**，不要写 `QUESTION: 无`。这些现有标记参与状态分类，QUESTION 优先产生 `needs_decision`；Codex 自行解决常规问题，只有确需用户决定时才生成 DECISION REPORT。完成标记不代表验收批准。

使用 [紧凑结果模板](references/collaboration.md#结果模板)，列出变更、命令/退出码、验收满足与缺口、真实风险/异议及绝对证据路径；正文不复制长日志。task/session/round/status 由桥接与 task.json 提供，不让 DSH 猜测或另建状态机。

## 并行与子 agent

- **一个 bridge task = 一个 DSH 主 session**。主 session 负责协调、整合与最终汇报；子 agent 是否可用、能开多少并发，由 DSH 运行时决定，**桥接不设子 agent 数量**，是否实际可用，以运行时提供的工具和执行结果为准。
- 任务**已授权的范围内**，派单不限制 DSH 选方法/选工具：独立子任务可以直接用原生子 agent 并行跑，**不需要为每次委派单独向用户申请许可**；缺失必要的授权或用户决策时再询问。
- **并行写同一份文件必须协调**：独立工作并行，共享文件的写入串行化，或给每个写者隔离工作区（独立 worktree/目录）后再合并。
- 子任务**继承该任务的授权与沙箱边界**（同一 workspace、不得绕过被拒权限、不得用桥接绕过访问控制），可以按实际需要继续拆分，避免重复工作和无目的递归。子 agent 的结论仍需核查：主 session 必须**实际核验**后再汇报为可核查结果（命令、退出码、产物路径）——DSH 内部的核验**不能替代**评审方的独立验收（见下节）。
- **全新任务首轮会自动带最新 brief**，后续轮次原样发送调用方正文。续接收到过旧版限制的 session 时，在下一轮派单正文里明确解除旧限制；已收到新规则的会话沿用授权，无需每轮重申。子 agent 的详细过程在 Desktop 查看，协作日志记录主会话派单与结果摘要。

## 评审纪律（重要）

`review --approve` 在 task 锁内校验当前 task、session、round、同一次 run 的最终回复和成功 run_end，禁止历史轮次替代。只有 `awaiting_review` 或 `needs_decision` 可批准，证据缺失/冲突、未启动/准备中/运行中/失败/中断一律拒绝，且不写批准记录。普通 review 只记录意见，保留原执行状态；批准后仍禁止 send。

对 `needs_decision`，Codex 必须实际检查问题解决的文件、测试或其他可核查证据，在评审正文记录问题、证据位置、核查动作和结论后才可批准。桥接只校验执行证据的对应关系，不会通过关键词、非空文本或 DSH 自报推定问题已解决，也不伪称机器已经核验业务正确性。Codex 接手失败任务的证据可用普通 review 记录，本版不提供强制批准失败状态的旁路。

1. **DSH 的回复是待核查声明**。按风险独立验收：低风险文档核查 diff/目标内容及必要结构；中风险检查调用路径、相关测试和关键条件；高风险状态机、并发、持久化、恢复、权限/模型切换必须设计或审查独立探针，覆盖关键不变量与负面场景。DSH 自测不能替代验收。
   优先复用可追溯的已有测试日志，独立检查其对应候选版本、命令、退出码和产物；发现失败保留证据，修复后跑失败探针与受影响测试。完整回归按最终候选与影响范围决定；代码、环境及风险证据未变时不为第二份 PASS 重跑全套。Codex 自己改代码后也须运行受影响测试。
2. 保持用户原有的授权与沙箱限制：**绝不用 DSH 绕过一次拒绝**，不自动提权，不做未经授权的破坏性/外部动作。
3. 真正只有用户能拍板的问题才停下来问；常规设计决策 Codex 自己定。
4. 失败如实上报并返回 Codex 决策；不自动重试、静默接管或切换后端。Codex 检查现场后可在原授权内明确决定恢复或自行接手剩余工作，记录理由，避免重复副作用。
5. 出错会写进日志，**不会自动重试**。
6. 任务日志里不放凭据，不放认证 URL/token/cookie，也不放私有推理。

## 环境

- 默认 DSH CLI（**仅**在用户明确要求 headless 时使用）：`E:\Deepseek Harness\resources\runtime\cli\bin\dsh.cmd`（覆盖：`--dsh-cmd <绝对路径>` 或 `$DSH_CMD`）
- Desktop 连接描述文件：`~\.codex\dsh-collab\desktop-connection\endpoint.json`（覆盖：`--desktop-endpoint` 或 `$DSH_DESKTOP_ENDPOINT`）
- Desktop Electron 可执行文件：默认 `E:\Deepseek Harness\Deepseek Harness.exe`（覆盖：`--electron-exe` 或 `$DSH_DESKTOP_EXECUTABLE`）
- 隔离测试：`--state-root <目录>`

## 对用户的进度与交付

首次使用时说明正在采用本技能。创建任务后尽早给用户 task ID 和 conversation.md 链接；长调用期间读取日志提供有意义的进度。续接只传未满足的验收条件、客观证据与必要边界。不要要求用户为已授权的常规步骤重复确认。最终回复附可读协作日志链接。

续接旧任务时先用 list/show 找到用户指定任务，读取 conversation.md 和实际项目状态，再用明确的 task ID 继续。历史任务（没有 `backend` 字段）按 headless 处理，保持兼容。

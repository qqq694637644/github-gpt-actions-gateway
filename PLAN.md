# GitHub GPT Actions Gateway 个人版破坏式重构计划

## 1. 设计前提

本项目只面向个人单机使用，典型场景是同时打开 3–5 个网页版 GPT，让它们分别维护不同仓库、不同 workspace 或不同分支。

固定前提：

```text
单台 Windows 主机
单个 Gateway 服务实例
单个 Uvicorn worker
多个网页版 GPT Action 客户端
不考虑企业、多租户或分布式部署
```

设计原则：

1. 默认允许并发，不设置没有必要的限制。
2. 不设置全局命令数量限制、workspace 命令数量限制或排队容量。
3. 不保留本地请求频率限制。
4. 不使用 mirror。
5. 不建设多实例、分布式锁、权限分级或复杂调度系统。
6. 所有公开 Action 都保持：

   ```text
   x-openai-isConsequential = false
   ```

7. 公开 OpenAPI `operationId` 总数必须不超过 30。
8. 允许破坏式升级，不兼容旧 `workspaceExecPwsh` 契约。

---

## 2. 本次重构要解决的问题

### 2.1 长命令失去控制

当前同步调用链是：

```text
GPT Action HTTP 请求
→ workspace 文件锁
→ pwsh
→ pytest / python / node
→ communicate()
```

客户端连接先失败后，后端命令仍可能继续运行，workspace 一直 busy，GPT 无法查询、取消或读取日志。

### 2.2 Timeout 不是硬截止

当前 `asyncio.wait_for(proc.communicate())` 在超时后可能继续等待取消完成。

如果后代进程继承 stdout/stderr：

```text
timeout 到达
→ communicate 取消迟迟不能完成
→ API 超过 timeout 很久才返回
```

### 2.3 Windows 没有可靠终止完整进程树

当前 `proc.kill()` 只终止顶层 `pwsh.exe`。

pytest、Python、Node、浏览器 driver 或 `Start-Process` 启动的后代可能继续运行。

### 2.4 Workspace 锁不可观察、不可恢复

普通 `lock` 文件只能表达“存在”，不能表达：

- 正在运行什么；
- operation ID；
- 启动时间；
- deadline；
- PID；
- 是否仍存活；
- 如何取消。

Gateway 卡住时，连 `workspaceStatus` 都无法正常诊断。

### 2.5 多个 GPT 共用限流桶

当前请求限流会让多个网页版 GPT 共用同一个桶。异步 operation 上线后，状态和日志查询会进一步增加请求数量。

个人本地使用不需要这种请求限流。

---

## 3. 最终架构

```text
网页版 GPT Action
        ↓
FastAPI Action API
        ↓
Workspace Service
        ↓
Command Manager
        ↓
Windows Process Supervisor
        ↓
pwsh / pytest / python / node / build
```

数据目录：

```text
data/
├── workspaces/
│   └── ws_<task>_<random>/
│       ├── repo/
│       └── workspace.json
└── operations/
    └── op_<id>/
        ├── state.json
        ├── stdout.log
        └── stderr.log
```

彻底移除：

```text
data/mirrors/
workspace lock 文件
同步 workspaceExecPwsh
请求限流桶
```

---

## 4. 公开 Action 设计

当前公开 `operationId` 数量约为 28。

破坏式删除：

```text
workspaceExecPwsh
```

新增一个统一 Action：

```text
workspaceCommand
```

因此公开 `operationId` 总数保持不变，仍不超过 30。

`workspaceCommand` 使用 discriminated union，通过 `action` 区分：

```text
start
get
logs
cancel
list
```

不为每种操作单独增加 OpenAPI operation。

所有公开 operation 必须继续标记：

```json
{
  "x-openai-isConsequential": false
}
```

OpenAPI 测试必须验证：

```text
operationId 总数 <= 30
所有 operation 都包含 x-openai-isConsequential=false
workspaceExecPwsh 不存在
workspaceCommand 存在
```

---

## 5. `workspaceCommand` 契约

### 5.1 启动命令

请求：

```json
{
  "action": "start",
  "idempotency_key": "cmd_5f94ac2e",
  "script": "python -m pytest",
  "timeout_seconds": 300,
  "max_output_bytes": 200000,
  "allow_network": false,
  "plain_output": true,
  "utf8_output": true
}
```

立即返回：

```json
{
  "operation_id": "op_8f13ad",
  "workspace_id": "ws_fix_timeout_73f901",
  "state": "running",
  "started_at": "2026-07-13T18:00:00Z",
  "deadline_at": "2026-07-13T18:05:00Z"
}
```

HTTP 请求不等待命令结束。

### 5.2 幂等规则

`idempotency_key` 对 `start` 必填。

规则：

```text
相同 key + 相同请求
→ 返回已有 operation_id

相同 key + 不同请求
→ IDEMPOTENCY_KEY_REUSED
```

Operation 状态文件、运行任务登记和幂等映射必须在同一个临界区内完成。

这样可以解决：

```text
服务端已经启动命令
→ 响应发送前连接中断
→ GPT 重试 start
→ 不会重复启动第二个命令
```

### 5.3 查询状态

请求：

```json
{
  "action": "get",
  "operation_id": "op_8f13ad"
}
```

状态只保留必要集合：

```text
running
succeeded
failed
timed_out
canceled
interrupted
```

返回：

```json
{
  "operation_id": "op_8f13ad",
  "workspace_id": "ws_fix_timeout_73f901",
  "state": "running",
  "exit_code": null,
  "started_at": "...",
  "deadline_at": "...",
  "finished_at": null,
  "duration_ms": 18342,
  "stdout_bytes": 18231,
  "stderr_bytes": 442
}
```

### 5.4 读取日志

请求：

```json
{
  "action": "logs",
  "operation_id": "op_8f13ad",
  "stdout_offset": 0,
  "stderr_offset": 0,
  "max_bytes": 50000
}
```

返回：

```json
{
  "stdout": "...",
  "stderr": "...",
  "next_stdout_offset": 18321,
  "next_stderr_offset": 442,
  "stdout_eof": false,
  "stderr_eof": false
}
```

日志通过 offset 增量读取，不要求 GPT 高频轮询。

建议 GPT 行为：

```text
短命令：数秒后查询一次
测试或构建：10–20 秒查询一次
只有需要诊断时才读取 logs
```

Gateway 不强制轮询频率。

### 5.5 取消命令

请求：

```json
{
  "action": "cancel",
  "operation_id": "op_8f13ad"
}
```

行为：

```text
设置 cancel 请求
→ 终止 Windows Job Object
→ taskkill /T /F 补充清理
→ proc.kill() 最后兜底
→ 限时关闭 reader
→ 写入 canceled
```

重复 cancel 返回当前状态，不报错。

### 5.6 列出 Operation

请求：

```json
{
  "action": "list",
  "workspace_id": "ws_fix_timeout_73f901",
  "state": "running"
}
```

用于在客户端调用异常后查找 operation。

---

## 6. Operation 数量与历史记录

“最多 30 个”只用于限制公开 OpenAPI `operationId`，不限制运行命令数量或历史记录数量。

不设置：

```text
全局最多运行命令数
每个 workspace 最多运行命令数
命令队列容量
Operation 历史数量上限
OPERATION_LIMIT_REACHED
COMMAND_CAPACITY_REACHED
```

不同 workspace 和同一 workspace 的命令都可以同时运行，由本机 CPU、内存和磁盘自然决定并发能力。

Operation 历史只做后台清理，不参与准入：

```text
终态记录默认保留 7 天
清理时间可配置
清理只删除终态 operation
运行中 operation 永不因数量被删除
```

Workspace prune 必须先查询 operation 状态。只要某个 workspace 仍关联至少一个 `running` operation，就必须跳过该 workspace，不得按 TTL、最后访问时间或目录年龄清理。终态 operation 不阻止正常 prune。

日志落盘使用每个 operation 的 `max_output_bytes`：

```text
达到上限后停止写入日志文件，但 reader 必须继续读取 stdout/stderr 并丢弃后续字节
记录 stdout_truncated / stderr_truncated
命令本身继续运行
```

不能因为日志已达到上限就停止读取管道，否则子进程可能因 stdout/stderr 管道写满而阻塞，重新造成命令无法退出。

不会因为历史记录数量拒绝新任务。

---

## 7. 并发规则

Gateway 默认允许所有 `workspaceCommand` 并发，包括同一 workspace 中的多个命令。

例如，同一 workspace 可以同时运行：

```text
pytest
ruff
mypy
多个定向测试
构建或分析脚本
```

Command Manager 不维护：

```text
workspace_id → 唯一 active_operation_id
workspace 级 command semaphore
shared / exclusive command 模式
command queue
```

而是维护：

```text
workspace_id → active_operation_ids[]
```

`workspaceStatus` 返回该 workspace 的全部活动 operation：

```json
{
  "workspace_id": "ws_fix_timeout_73f901",
  "active_operations": [
    {
      "operation_id": "op_test",
      "state": "running",
      "script_summary": "python -m pytest"
    },
    {
      "operation_id": "op_lint",
      "state": "running",
      "script_summary": "python -m ruff check ."
    }
  ]
}
```

文件、Git、PR、CI 和发布操作使用各自已有的专用 Operation 和校验机制。`workspaceCommand` 不额外推断脚本是否会修改 Git、虚拟环境、构建目录或其他共享状态，也不因此阻止并发。

如果两个命令或两个文件操作实际写入同一资源，结果由命令自身、文件系统或专用 Operation 返回真实错误。Gateway 不增加通用并发限制。

读操作、文件写入、补丁、测试、lint、构建、状态查询和日志读取均不因其他 command 运行而被 Gateway 阻止。

---

## 8. Workspace ID 与 Branch 策略

### 8.1 Workspace ID 只能由服务端生成

`prepareWorkspace` 请求模型彻底删除 `workspace_id` 字段。调用者不能在 prepare 请求中指定、复用或覆盖 workspace ID。

`prepareWorkspace` 必须要求调用者提供 `idempotency_key`。相同 key 与相同准备参数必须返回第一次创建的 workspace ID；相同 key 与不同参数必须返回 `IDEMPOTENCY_KEY_REUSED`。Workspace 目录创建、幂等映射和返回 ID 的登记必须在同一个小临界区内完成。

请求示例：

```json
{
  "idempotency_key": "prepare_fix_timeout_7f31c2",
  "base_ref": "main",
  "purpose_slug": "fix-timeout"
}
```

服务端生成：

```text
ws_fix_timeout_7f31c2
```

返回值必须包含最终 workspace ID。

该规则是破坏式更新，不提供客户端显式传入 workspace ID 的兼容路径。

后续操作必须使用 `prepareWorkspace` 返回的 workspace ID。继续同一任务或同一 PR 且已保存原 workspace ID 时，调用者直接使用该 ID 调用 `workspaceStatus`、文件、命令、提交等 Operation，不再次调用 `prepareWorkspace`。

如果客户端在响应丢失后不确定 workspace 是否已创建，必须使用原 `idempotency_key` 重试 `prepareWorkspace`，由服务端返回原 workspace ID，不能换 key 重复创建。

继续已有 PR 但没有可复用 workspace ID 时，调用者向 `prepareWorkspace` 提供 `source_pr_number` 和新的 `idempotency_key`，由服务端为该次任务生成新的 workspace ID，并从 PR head 准备工作区。

### 8.2 新任务默认唯一

每个新任务必须调用 `prepareWorkspace` 创建新的 workspace，不得复用其他任务的 workspace。服务端生成的 workspace ID 必须唯一。新任务建议同时使用唯一 branch：

```text
workspace: ws_fix_timeout_7f31c2
branch:    gpt/fix-timeout-7f31c2
```

只有明确继续同一任务或同一 PR 时，才允许继续使用已保存的旧 workspace；继续已有 PR 时才复用对应 branch。

### 8.3 同一远端 branch 冲突

不同 workspace 可以指向同一远端 branch，但提交时必须继续使用：

```text
expected_head_sha
```

如果远端 branch 已被另一个 workspace 更新：

```text
BRANCH_HEAD_CHANGED
```

不得强制覆盖，也不得自动 rebase 或 force push。

这是现有 Git Operation 的并发校验，不由 `workspaceCommand` 管理。

---

## 9. 删除请求限流

彻底删除或默认永久关闭当前每分钟请求限流。

删除：

```text
rate_limit_per_minute
client_host + token bucket
RATE_LIMITED
```

个人本地 Gateway 不限制：

- Action 调用次数；
- 状态查询次数；
- 日志读取次数；
- 同时连接的 GPT 页面数量。

GitHub 官方 API 自身返回的限流仍需原样传递，但 Gateway 不额外增加本地限流。

GitHub 错误应尽量保留：

```text
HTTP status
GitHub message
request ID
rate-limit remaining
rate-limit reset
```

对安全的 GitHub GET 查询可以做一次短暂重试：

```text
连接重置
临时 502/503/504
```

不重试写操作。

---

## 10. 单实例保证

内存中的 operation registry 和异步任务只适用于单进程。

因此 Gateway 启动时必须获取一个 Windows named mutex：

```text
Global\GitHubGptActionsGateway
```

第二个实例或多 worker 启动时立即失败并给出清晰错误：

```text
GATEWAY_INSTANCE_ALREADY_RUNNING
```

明确禁止：

```text
uvicorn --workers 2
gunicorn 多 worker
同时启动两个 Windows 服务实例
```

Named mutex 会在进程退出后由 Windows 自动释放，不会形成 stale workspace lock。

这不是多实例系统，只是保证个人版的单进程假设真实成立。

---

## 11. Windows Process Supervisor

### 11.1 使用 Windows Job Object

每个 command 创建独立 Job Object，并设置：

```text
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
```

使用标准库 `ctypes` 实现，不增加 pywin32 依赖。

启动流程：

```text
创建 Job Object
→ 启动 pwsh
→ 将 pwsh 加入 Job
→ 启动 stdout reader
→ 启动 stderr reader
→ 等待进程、timeout、cancel 或 shutdown
```

这样即使：

```text
pwsh 提前退出
pytest 继续运行
Python 启动 detached child
Node/browser driver 仍存活
```

后代仍受 Job Object 生命周期约束。

### 11.2 终止顺序

```text
TerminateJobObject
→ 等待固定 grace
→ taskkill /PID root_pid /T /F
→ proc.kill()
→ 取消 stdout/stderr reader
→ 关闭 transport
→ 写入终态
```

每一步都有独立最大等待时间。

任何日志管道问题都不能阻止 operation 进入终态。

### 11.3 不再使用 `communicate()` 管理生命周期

删除：

```python
await asyncio.wait_for(proc.communicate(), timeout=...)
```

改为独立任务：

```text
process_wait_task
stdout_reader_task
stderr_reader_task
timeout_task
cancel_event
shutdown_event
```

使用 `time.monotonic()` 计算：

```text
timeout
duration
cleanup duration
```

使用 UTC 时间记录：

```text
started_at
deadline_at
finished_at
```

---

## 12. Operation 状态持久化

### 12.1 原子写入

每次状态更新：

```text
写 state.json.tmp
→ flush
→ fsync
→ os.replace(state.json.tmp, state.json)
```

不能直接覆盖目标 JSON。

### 12.2 唯一终态

每个 operation 有自己的 `asyncio.Lock`。

该锁只保护单个 operation 的状态转换、幂等创建结果和日志元数据更新。它不锁整个 workspace，也不阻止同一 workspace 中其他 operation 并发运行。

终态转换只能发生一次：

```text
running → succeeded
running → failed
running → timed_out
running → canceled
running → interrupted
```

timeout 与 cancel 同时发生时，先获取 operation lock 的路径决定终态。

### 12.3 状态内容

至少记录：

```text
operation_id
idempotency_key
workspace_id
script_sha256
state
root_pid
job_id 或 job handle metadata
started_at
deadline_at
finished_at
duration_ms
exit_code
stdout_bytes
stderr_bytes
stdout_truncated
stderr_truncated
error_code
error_message
```

---

## 13. Gateway 重启与恢复

### 13.1 Shutdown

Gateway 正常退出时：

```text
关闭所有活动 Job Object
→ 等待固定 cleanup 时间
→ 未完成 operation 标记 interrupted
→ 退出
```

### 13.2 Startup

启动时扫描 operation 状态目录。

旧状态为：

```text
running
```

统一改为：

```text
interrupted
```

并记录：

```text
error_code = gateway_restarted
```

不尝试恢复旧进程。

同时：

```text
清理旧版本遗留 workspace lock 文件
保留 workspace 和未提交修改
重新建立空的 operation registry
```

重启后所有 workspace 默认可用。

---

## 14. 删除 Mirror

彻底删除：

```text
WORKSPACE_MIRROR_ROOT
data/mirrors
mirror clone
mirror fetch
mirror lock
mirror diagnostics
mirror tests
```

每个 workspace 直接拥有完整 Git clone：

```text
prepare new workspace
→ git clone repository workspace/repo

prepare existing workspace
→ git fetch origin
→ 按模式 checkout / refresh
```

3–5 个 GPT 同时 prepare 时，各自操作独立目录，不共享 Git object store。

允许它们并发 clone，不设置 repo 级锁或 prepare 队列。

磁盘和网络开销接受，以换取实现简单和失败隔离。

---

## 15. Schema 版本与破坏式升级

所有响应增加：

```text
X-Gateway-Schema-Version
```

OpenAPI 根信息也包含：

```text
gateway schema version
minimum supported prompt version
```

旧 `workspaceExecPwsh` 路径直接删除，不保留兼容实现，也不增加旧逻辑 wrapper。

升级说明必须明确：

```text
重新导入 GPT Action OpenAPI
刷新或关闭旧 GPT 会话
使用更新后的 PROMPT.md
```

旧网页版 GPT 会话仍保留旧工具定义时，可能调用不存在的旧路径。这是破坏式升级的预期行为，不在后端保留兼容代码。

---

## 16. 根目录提示词与文档修改

### 16.1 重写 `PROMPT.md`

`PROMPT.md` 是 GPT 的主要工作规则，必须同步更新。

新增规则：

#### Workspace 创建

```text
prepareWorkspace 不接受 workspace_id，workspace ID 必须始终由服务端生成。
每个新任务必须使用新的 idempotency_key 调用 prepareWorkspace，获得新的 workspace。
保存返回的 workspace_id，后续操作继续使用它。
只有继续同一任务或同一 PR 时，才使用保存的旧 workspace_id。
prepareWorkspace 响应异常时使用原 idempotency_key 重试，不能换 key 重复创建。
继续已有 PR 且没有保存的旧 workspace_id 时，传 source_pr_number，由服务端生成新的 workspace_id。
```

#### PowerShell 使用范围

```text
workspaceCommand 中的 pwsh 主要用于测试、构建、lint、类型检查、依赖安装、诊断和复杂脚本。
源码修改优先使用 workspaceApplyPatch 或 workspaceWriteFile。
Git 提交、push、checkout、reset、分支和 PR 操作必须使用对应专用 Operation，不通过 pwsh 替代。
源码阅读优先使用 workspaceInspect、workspaceSearch 和 workspaceReadFiles；当这些工具不足以表达复杂筛选、批量统计或动态分析时，可以灵活使用 pwsh 查看和分析源码。
```

#### 长命令

```text
workspaceCommand(action=start)
→ 保存 operation_id
→ workspaceCommand(action=get)
→ 必要时 workspaceCommand(action=logs)
→ 完成后 workspaceStatus / workspaceDiff
```

#### Start 调用异常

```text
start 返回 ClientResponseError 或连接异常
→ 不生成新的 idempotency_key
→ 使用原 idempotency_key 重试 start
→ 或 workspaceCommand(action=list) 查询
→ 相同请求必须返回原 operation_id
```

#### 并发规则

```text
不同 workspace 和同一 workspace 都允许多个 command 并发。
Gateway 不设置全局或 workspace 级 command 数量限制。
workspaceStatus 返回全部 active_operations。
每个 operation 可以独立查询、读取日志和取消。
```

#### 删除旧规则

删除：

- `workspaceExecPwsh`；
- 同步等待长命令；
- mirror；
- 请求限流；
- 全局 command 上限；
- workspace command 上限；
- command queue；
- operation history 数量上限；
- 基于 workspace busy 阻止 command 的规则。

### 16.2 重写 `README.md`

README 只描述个人单机版：

- 单实例；
- Windows；
- 3–5 个 GPT 并发；
- 无本地请求限流；
- 不限制不同 workspace 或同一 workspace 的并发命令；
- Job Object 进程树；
- `workspaceCommand`；
- 幂等；
- 重启恢复；
- 无 mirror；
- 所有 Action 均为 `x-openai-isConsequential=false`；
- 公开 operationId 不超过 30；
- 破坏式升级步骤。

### 16.3 删除旧历史计划

删除已经完成或不再适用的历史计划文档，例如：

```text
REMOVE_NON_GPT_PR_WORKSPACE_LIMIT_PLAN.md
```

避免 GPT 将旧计划误认为当前规则。

---

## 17. 实施阶段

### 阶段一：进程执行器

实现：

- Windows Job Object；
- stdout/stderr 独立 reader；
- monotonic deadline；
- cancel；
- shutdown；
- 有界 cleanup；
- atomic operation state；
- bounded logs。

### 阶段二：Command Manager

实现：

- operation registry；
- required idempotency key；
- prepareWorkspace idempotency；
- start/get/logs/cancel/list；
- 同一 workspace 多 operation；
- 重启 interrupted 恢复；
- 不设全局并发限制；
- 不设 workspace 并发限制；
- 不设 queue。

### 阶段三：破坏式 API 替换

- 删除 `workspaceExecPwsh`；
- 新增 `workspaceCommand`；
- 修改 `workspaceStatus`，返回全部 active operations；
- 保持 operationId 总数不超过 30；
- 所有 operation 保持 `x-openai-isConsequential=false`。

### 阶段四：Workspace 简化

- 删除文件 lock；
- 删除 mirror；
- 删除 `prepareWorkspace` 请求中的 `workspace_id`，改为服务端强制生成；
- prune 在 workspace 存在 running operation 时必须跳过；
- 保留现有专用 Git Operation 的 `expected_head_sha` 校验。

### 阶段五：删除限流并更新文档

- 删除本地请求限流；
- 更新 PROMPT；
- 更新 README；
- 删除旧历史计划；
- 重新导出 OpenAPI。

---

## 18. 必须增加的测试

### Process Supervisor

```text
timeout=2 秒，返回时间不超过 2 秒 + cleanup grace
pwsh 启动 Python 后，timeout 终止两者
pwsh 提前退出、Python 后代继续运行，Job Object 仍能终止后代
pytest 启动 Node 后，cancel 终止完整树
后代继承 stdout/stderr，不延长硬 deadline
reader 不结束时，operation 仍进入终态
日志达到 max_output_bytes 后继续 drain 并丢弃，不因管道写满卡住
```

### Operation

```text
start 立即返回 operation_id
相同 idempotency_key 返回相同 operation
相同 key 不同请求返回 IDEMPOTENCY_KEY_REUSED
get 可以查询 running 和终态
logs 支持 offset
cancel 可重复调用
timeout 与 cancel 竞争时只有一个终态
状态 JSON 写入中崩溃不会留下半截 JSON
operation 小锁只序列化状态、幂等和日志元数据，不阻止同 workspace 其他 operation
```

### Prepare 与 prune

```text
相同 prepare idempotency_key 和相同参数始终返回同一 workspace_id
相同 prepare key 和不同参数返回 IDEMPOTENCY_KEY_REUSED
每个新任务使用新 key 获得新 workspace
继续同一任务时使用已保存 workspace_id
存在 running operation 的 workspace 不会被 prune
operation 全部终态后 workspace 可按正常 TTL 规则 prune
```

### Command 并发

```text
5 个不同 workspace 同时 start，全部运行
同一 workspace 5 个 start，全部运行
不出现全局容量错误
不出现 workspace command 容量错误
不出现本地 429
workspaceStatus 返回全部 active_operations
每个 operation 可独立 get/logs/cancel
一个 operation 结束或取消不影响同 workspace 的其他 operation
```

### Git 并发

```text
5 个 workspace 同时 clone 同一 repo
不使用 mirror
互不产生 Git lock 冲突
不同 workspace 修改同一 branch
后提交者得到 BRANCH_HEAD_CHANGED
不会 force push
```

### Gateway 生命周期

```text
两个 Gateway 实例启动，第二个被 named mutex 拒绝
多个命令运行时重启 Gateway
所有 Job Object 被关闭
operation 变为 interrupted
workspace 未提交修改保留
遗留旧 lock 文件被清理
```

### OpenAPI

```text
operationId 总数 <= 30
workspaceCommand 存在
workspaceExecPwsh 不存在
所有 operation x-openai-isConsequential=false
schema version 存在
```

### 真实网页版验收

同时打开 3–5 个 GPT：

```text
每个 GPT 可使用独立或相同 workspace
同时执行 prepare、inspect、edit、test、diff、commit 或 CI 查询
同一 workspace 可同时运行多个测试、lint 和构建命令
```

验收期间不应出现：

```text
本地 RATE_LIMITED
全局 OPERATION_LIMIT_REACHED
workspace command 容量限制
全局 COMMAND_CAPACITY_REACHED
只能通过重启 Gateway 恢复的 workspace
```

---

## 19. 最终验收标准

1. 不再存在本地请求限流。
2. 不限制不同 workspace 的活动命令数量。
3. 不限制同一 workspace 的活动命令数量。
4. 不使用 command queue。
5. 不使用全局或 workspace command semaphore。
6. `workspaceStatus` 返回全部活动 operation。
7. 长命令通过异步 operation 管理。
8. `start` 必须幂等。
9. timeout 和 cancel 使用 Windows Job Object 终止完整进程树。
10. 所有 cleanup 都有最大等待时间。
11. 不再用 `communicate()` 作为生命周期控制。
12. Operation JSON 原子写入。
13. Operation 级小锁只保护状态转换、幂等创建和日志元数据，不锁 workspace。
14. Gateway 重启后 operation 变为 interrupted。
15. 重启不丢失 workspace 未提交修改。
16. 删除 workspace 文件锁。
17. prune 跳过仍有 running operation 的 workspace。
18. 删除 mirror。
19. `prepareWorkspace` 不接受客户端 workspace ID，所有 workspace ID 均由服务端生成。
20. `prepareWorkspace` 和 `workspaceCommand start` 都必须幂等。
21. 新任务必须创建新 workspace，只有继续同一任务或 PR 时才复用旧 workspace。
22. 同一远端 branch 继续使用现有 `expected_head_sha` 防止覆盖。
23. 公开 operationId 不超过 30。
24. 所有 Action 保持 `x-openai-isConsequential=false`。
25. `PROMPT.md`、`README.md` 和 OpenAPI 与新模型一致。
26. 3–5 个网页版 GPT 可以同时完成维护任务。
27. 同一 workspace 的多个命令可以默认并发运行。

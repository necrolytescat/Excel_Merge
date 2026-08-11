# M3 版本监控性能诊断（第一份交付）

> 归档状态：优化前静态诊断；真实结论见 `docs/archive/m3-history/M3-VERSION-MONITORING-PERFORMANCE-STATUS.md`
> 更新日期：2026-08-11
> 基线：`r26475 -> r26514`，197 个工作簿，116 条最终净变化，0 个错误

## 1. 范围与硬约束

本轮只解释现状、定位可能的耗时放大点，并设计可测量的分段计时。没有修改 Runner、SVN、快照、Diff、归因、报告、数据库或调度代码，也没有访问真实 SVN。

后续无论采用何种优化，都必须保持：

- 报告区间仍由逻辑边界决定，且为左开右闭 `(start_at, end_at]`；
- 时间边界仍换算为固定 Revision，Excel `main` 和 TableCsv 必须来自同一固定 Revision；
- 只比较 `main` 清单中 `isExport=1` 的 Sheet，业务值仍以 TableCsv 为准；
- 最终净变化、字段级作者归因、manifest 语义和固定分支身份不变；
- `m3.monitor-report.v1`、publication、latest、30 天治理和 Windows 调度语义不变；
- SVN 仍只允许 `info / log / list / cat` 等只读操作。

任何真实 SVN 测量前，必须先向用户说明：目标固定分支、Revision 范围、预计只读命令种类、是否使用现有缓存或隔离缓存、是否会产生报告/数据库写入、预计持续时间和停止条件。未经说明和确认，不执行真实测量。

## 2. 当前一份报告到底比较了什么

### 2.1 先固定时间，不按 Runner 实际启动时间取数

Windows 任务只负责唤醒独立 Runner。报告的起止时间已经保存在 Run 中，Runner 晚启动、重试或运行二十分钟，都不会改变本次区间。

正常报告比较“上一个逻辑边界之后，到本次逻辑截止点为止”的变化，即 `(start_at, end_at]`。正好发生在起点的提交不算，正好发生在截止点的提交要算。

### 2.2 把两端时间分别换成固定 Revision

Runner 先确认当前访问到的 SVN 分支仍是任务创建时冻结的同一个仓库、同一个规范 URL 和同一个仓库相对路径。然后分别查询：

- 在 `start_at` 时刻，这个分支最后有效的 Revision；
- 在 `end_at` 时刻，这个分支最后有效的 Revision。

得到的两个编号就是本次比较的固定起点和固定终点。基线报告得到 `r26475 -> r26514`。Revision 是整个仓库的全局编号，中间有编号空缺是正常的。

### 2.3 在起点和终点各重建一份完整业务快照

对一个固定 Revision，当前实现会先递归列出固定分支下的路径，再完成以下工作：

1. 找到 Table 目录下的全部 Excel 工作簿；
2. 逐个读取工作簿字节；
3. 解析每个工作簿的 `main` Sheet；
4. 只保留 `isExport=1` 的清单行，并取得 `sheetName` 和 `tbxName`；
5. 用 `tbxName` 在同 Revision 的同级 TableCsv 目录中唯一匹配 CSV；
6. 读取并解析 CSV 的展示名、字段名、类型、scope、主键和业务行；
7. 排除 `scope=none`，按类型规范化整数、小数、布尔和时间值。

Excel 在这里决定“哪些 Sheet 属于业务快照”，TableCsv 提供“这些 Sheet 的实际业务值”。CSV 文件即使存在，只要当时没有被 Excel `main` 清单纳入导出，就不能参加比较。

### 2.4 只计算起点到终点仍然存在的净变化

起止快照按“工作簿 -> `sheetName` -> 业务主键 -> `field_name`”匹配。输出包括字段值修改、行新增/删除、字段新增/删除和字段定义修改。

这里不报告区间中每一次来回修改，只报告截止时相对起点仍然不同的结果。行号、文件哈希和模糊相似度都不作为业务身份。

例如：

```text
r100 起点：角色 100 的 HP = 100
r101 小王：HP 改成 120
r102 小李：HP 又改回 100
r102 终点：角色 100 的 HP = 100
```

起点和终点相同，因此最终净变化是 0，报告中不会出现这条字段。系统不是漏掉了 r101，而是产品定义明确要求只报告截止时仍存在的变化。

如果 r102 最终是 110，则报告为 `100 -> 110`，最终修改人归给形成 110 的 r102，而不是曾经形成 120 的 r101。

### 2.5 再按区间提交逐次回放，给最终变化找作者

最终净变化确定后，系统读取 `(start_at, end_at]` 内固定分支实际相关的提交，并按 Revision 升序处理。归因阶段会：

1. 再加载一次起点完整快照；
2. 为区间内每个相关提交加载该 Revision 的完整快照；
3. 比较相邻两次快照，形成字段、行和结构事件账本；
4. 对每一条最终净变化，寻找最后一个真正形成最终状态的事件；
5. 使用该事件所在提交的作者、Revision、时间和说明。

因此，后来有人提交了同一个文件但没有改变目标字段，不会抢走目标字段的作者。无法把最终变化可靠连接到事件时，必须标为 `unresolved` 并生成 partial，不能猜作者。

### 2.6 最后构建并发布报告

系统对变化和错误稳定排序，反算统计，生成规范 JSON 和单文件离线 HTML，并分别计算 SHA-256。发布顺序是：

1. 数据库准备 publication；
2. 原子写入不可变 history JSON；
3. 原子写入不可变 history HTML；
4. 在任务锁内原子更新 `latest.html`；
5. 数据库最终激活 publication 和 Run。

完全失败不能覆盖旧 latest。成功、无变化和 partial 才能形成正式报告。

## 3. 当前调用次数与耗时放大点

以下记号用于描述一次 Run：

- `C`：左开右闭区间内，固定分支实际相关的提交数；
- `W_r`：Revision `r` 下的 Excel 工作簿数，基线两端为 197；
- `E_r`：Revision `r` 下，全部 manifest 条目引用的唯一 TableCsv 路径数；
- `U`：本次实际出现的不同快照 Revision 数。

### 3.1 Runner 装配与分支检查

当前正常路径会执行两次分支身份查询：Engine Factory 创建时一次，Engine 执行时又一次。Factory 还会在任务创建时的 bound Revision 对固定分支执行一次递归路径列表，用来定位 Table 目录。

可能耗时：

- 2 次 `svn info --xml`；
- 1 次额外 `svn list -R --xml`；
- 相同 Run 内分支身份验证存在重复，但这是静态诊断结论，当前不直接删除。

### 3.2 时间换算和提交列表

Engine 已经为起点、终点各查询一次日期 Revision。随后 `list_branch_commits` 内部又为相同起止时间各查询一次，再查询一次 Revision 范围日志。

因此当前一次正常 Run 的历史阶段通常产生：

- 4 次日期边界 `svn log -l 1`；
- 1 次区间 `svn log -v`；
- 共 5 次 log 调用。

重复换算必须测量，但后续即使复用 Revision，也仍要保留 XML 提交时间的精确 `(start_at, end_at]` 过滤。

### 3.3 完整快照请求次数

最终净值阶段请求起点和终点两份快照。归因阶段又请求一次起点，并为每个提交请求一份快照。

所以算法层面的完整快照请求数是：

```text
C + 3
```

每次快照都重新执行递归 `svn list -R`、遍历全部工作簿、解析 Excel `main`、解析 manifest 引用的 TableCsv。加上 Factory 定位 Table 的列表，一次 Run 的递归 list 总数是：

```text
C + 4
```

基线状态文档确认最终变化只归因到 r26509 和 r26514，但这不能证明区间提交列表恰好只有 2 条；`C` 必须在真实测量中记录。如果 `C=2`，当前会请求 5 份完整快照，并执行 6 次递归 list；197 个工作簿会触发约 `5 * 197 = 985` 次 Excel manifest 解析调用。

### 3.4 `cat` 缓存能省网络，但不能省全部重复工作

SVN 内容缓存键是 `(完整文件 URL, Revision)`：

- 同一进程再次读取同一路径、同一 Revision，会命中内存缓存；
- 新进程可能命中磁盘缓存；
- 同一路径换了 Revision，即使文件内容根本没变，也会成为新的缓存项并再次 `cat`；
- `info / log / list` 不进入该内容缓存；
- 缓存命中后，Excel 和 CSV 仍会从缓存字节重新完整解析。

冷缓存下，内容读取的理论上界接近：

```text
对每个不同快照 Revision r，读取 W_r 个 Excel + E_r 个唯一 TableCsv
```

即 `sum(W_r + E_r)` 次不同 `(path, revision)` 内容读取。当前读取循环是串行的。相同 CSV 若被多个 manifest 条目引用，外部 `cat` 可能因缓存只发生一次，但 CSV 解析仍可能重复发生。

### 3.5 SVN CLI 进程启动还有额外放大

当前底层每次 `_run` 或 `_run_raw` 都会先自动探测 SVN CLI，这通常会额外启动一次 `svn --version --quiet`。`info` 和 `list` 在 Provider 校验时还会先探测一次，然后正式执行前再探测一次。

因此应把“业务 SVN 命令数”和“实际子进程数”分开统计。冷缓存时，大量 `cat` 不只意味着网络读取，还意味着大量 CLI 探测和进程创建。

### 3.6 Excel 和 CSV 解析的 CPU/内存放大

manifest 首选 `openpyxl`，当前以 `read_only=False` 打开整个工作簿；只有异常时才回退到最小 OOXML 解析。即使业务上只读取 `main`，工作簿加载仍可能处理整本文件、样式和共享结构。

这个成本会被 `C+3` 份快照重复放大。内容缓存无法避免解析，且同一 Revision 的起点、终点重复请求也没有快照级缓存。

TableCsv 解析会把 CSV 全部解码为文本，再构造完整 records、字段、原始值和规范化值。大 CSV 在多个 Revision 上重复解析，会同时放大 CPU、临时内存和对象数量。

### 3.7 Diff 和归因本身的放大

- 最终净值执行 1 次全快照语义 Diff；
- 提交回放执行 `C` 次相邻全快照语义 Diff；
- 当前没有根据 changed paths 缩小每次 Diff 的工作簿范围；
- 最终归因对每条最终变化扫描整个事件账本并保留最后匹配，复杂度近似 `最终变化数 * 事件数`。

基线最终变化为 116 条，归因匹配本身未必是主瓶颈，但必须单独计时，不能凭感觉排除。

### 3.8 报告构建、发布和治理

报告阶段会稳定排序、严格模型校验、生成规范 JSON、再次序列化 JSON 嵌入 HTML，并计算两份哈希。文件发布至少包含 history JSON、history HTML、latest HTML 三次原子写入和 fsync，其中 HTML 写两份。

此外，Runner 在处理任务或人工重试前，会先遍历全部监控任务做历史报告过期清理。历史文件多时，这部分也可能在真正计算前产生固定成本。

这些阶段相对 197 工作簿快照预计较小，但必须计时后才能下结论。

## 4. 分段计时与计数方案

### 4.1 观测原则

- 使用 `time.perf_counter()` 记录墙钟时间，使用 `time.process_time()` 记录进程 CPU 时间；
- 记录阶段开始/结束、次数、读取字节和结果计数，不记录 SVN URL、凭据、物理路径、stderr 或文件内容；
- 计时数据写入独立诊断日志，不进入 `m3.monitor-report.v1`，不改变报告 SHA、Store 状态机或调度；
- 所有计数器按单个 Run 聚合，逐工作簿明细只保留最慢 Top N，避免日志本身放大耗时；
- 计时器和计数器必须可关闭，关闭时不改变原执行路径；
- 同时记录进程工作集峰值。内存采样与详细逐文件计时会有开销，先测量观测开销，再决定正式采样粒度。

### 4.2 Runner 顶层阶段

每次 Run 至少记录：

| 阶段 | 指标 |
|---|---|
| `maintenance` | 耗时、扫描任务数、扫描/删除报告数 |
| `engine_factory` | 耗时、身份查询次数、Table 定位 list 次数 |
| `branch_verify` | 耗时、info 次数 |
| `resolve_interval_revisions` | 起点/终点分别耗时、结果 Revision |
| `net_diff_total` | 起止快照加载和最终 Diff 总耗时 |
| `history_commits` | 两次内部 Revision 换算、区间 log、提交数 `C` |
| `attribution_total` | 回放快照、事件 Diff、账本匹配总耗时 |
| `report_render` | 排序/模型校验、JSON、HTML、哈希耗时和字节数 |
| `publication_prepare` | Store prepare 耗时 |
| `publish_history` | JSON/HTML 各自写入、fsync、replace 耗时 |
| `activate_latest` | 锁等待、旧 latest 读取校验、写入耗时 |
| `publication_finalize` | Store finalize 耗时 |
| `run_total` | 总墙钟、总 CPU、峰值工作集 |

### 4.3 每份快照的分段

每次 `load_snapshot(revision)` 记录：

| 分段 | 指标 |
|---|---|
| `snapshot.list_tree` | 耗时、返回路径数、XML 字节数 |
| `snapshot.index_paths` | 耗时、Excel 数、CSV 数 |
| `snapshot.read_workbooks` | 请求数、内存/磁盘命中、冷 miss、读取字节、耗时 |
| `snapshot.parse_manifests` | 调用数、输入字节、openpyxl/OOXML 次数、失败数、耗时 |
| `snapshot.read_csv` | 请求数、唯一文件数、各类缓存命中、读取字节、耗时 |
| `snapshot.parse_csv` | 调用数、输入字节、字段数、数据行数、失败数、耗时 |
| `snapshot.total` | 总耗时、工作簿数、Sheet 数、错误数 |

必须同时记录“快照请求次数”和“不同 Revision 数”，这样才能直接看到起点/终点重复加载的成本。

### 4.4 SVN 最底层计数

在 Provider/Client 边界按命令类型聚合：

- `info / log_date / log_range / list_recursive / cat / version_probe` 次数；
- 每类总耗时、最大耗时、失败/超时数；
- stdout 原始字节数；
- `cat` 请求数、memory hit、disk hit、miss、write 及读取字节；
- 子进程启动总数。

命令参数只保留安全分类和 Revision，不记录目标 URL 或本机缓存路径。

### 4.5 Diff 与归因计数

记录：

- 最终 Diff 的工作簿、Sheet、行和字段遍历数；
- 每次提交回放的 Revision、changed path 数、快照耗时、事件 Diff 耗时和事件数；
- 事件账本总长度；
- 最终变化数、归因成功/unknown/unresolved 数；
- 最终变化连接账本的总耗时。

changed paths 初期只用于观测，不用于跳过工作簿或改变 Diff 范围。

## 5. 测量批次与一致性门禁

### 5.1 第一批：本地无 SVN 观测开销

先用现有 Mock/单元/集成夹具验证：

- 开关关闭时结果字节不变；
- 开关开启时最终变化、归因和错误不变；
- 计数公式与受控提交数一致；
- 计时日志不包含 URL、路径、凭据、原始数据或异常堆栈；
- 观测开销在小夹具上可接受。

### 5.2 第二批：真实基线的被动只读测量

得到用户确认后，只读测量 `r26475 -> r26514`。第一遍不清理也不改变现有共享缓存，记录当前真实缓存状态。不得为了计时修改任务边界、重发正式报告或覆盖 latest。

一致性必须满足：

- start Revision = 26475，end Revision = 26514；
- workbook count = 197，reliable workbook count = 197；
- change count = 116；
- error count = 0；
- unknown author = 0，unresolved = 0；
- 每条变化的工作簿、Sheet、类型、主键、字段、前后值、作者和 Revision 与基线一致；
- 结果使用稳定语义指纹比较；若复用原 Run/固定 generated_at，则再比较既有规范报告 SHA。

### 5.3 第三批：隔离冷/暖缓存对照

只有用户再次确认后，才使用独立的新缓存目录做冷缓存测量；不能清空共享缓存。随后对同一固定输入做一次暖缓存测量。两次均只读 SVN，且应使用不发布、不更新 MonitorStore 的诊断入口。

对照至少报告：

- 总墙钟、CPU、峰值工作集；
- 每阶段耗时占比；
- SVN 业务命令数、子进程数和读取字节；
- cache hit/miss；
- `C`、`C+3` 快照请求数、不同 Revision 数；
- Excel/CSV 解析次数和输入字节；
- 116 条结果一致性门禁。

## 6. 当前优先验证的瓶颈假设

按静态证据排序，尚未把任何一项当作最终结论：

1. **逐提交完整快照回放**：`C+3` 次全量快照把 197 工作簿和全部导出 CSV 的读取/解析成倍放大；
2. **Excel 全工作簿重复解析**：`openpyxl(read_only=False)` 即使只取 `main`，仍可能是主要 CPU 和内存来源；
3. **缓存按 Revision 隔离**：文件未变化但 Revision 不同仍重新 `cat`，冷缓存网络量接近每个 Revision 一套完整数据；
4. **串行内容读取与频繁 CLI 探测**：每个冷 miss 串行执行，并可能伴随一次 `svn --version` 子进程；
5. **重复 list / 日期 Revision / 身份验证**：不会单独解释全部二十分钟，但调用稳定重复，容易量化；
6. **全量事件 Diff 与账本匹配**：提交数和事件数较大时可能成为第二级 CPU 热点；
7. **报告构建、原子发布和运行前治理**：预计占比较低，但必须用数据确认。

在真实分段数据出来之前，不实施快照缓存、增量回放、并发拉取、Parser 替换或命令复用。

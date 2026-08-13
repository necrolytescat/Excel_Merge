# 版本对比性能优化批次 2 验收

## 1. 范围

本批次仅优化 `SVNWorkbookDatasetResolver` 对冻结目录事实的重复查询。未实施 CSV 并行读取、清单解析复用或 core 解析策略修改。

缓存覆盖：

- 每个冻结端点的 TABLE 递归发现结果；
- 已确定 TABLE 后，同级 TableCsv 目录发现结果；
- 成功查询但未发现目录的结果。

缓存不包含工作簿或 CSV 内容，不写磁盘，不改变 SVN Provider 缓存。

## 2. 缓存契约

缓存 key 包含事实类型、endpoint id、规范化 URL、具体 Revision、物理路径配置指纹、TABLE/TableCsv 布局名，以及 TableCsv 查询对应的 TABLE 实际路径。

实现为 Resolver 实例内、线程安全、总容量 256 的 LRU。并发相同 key 采用 single-flight；Provider 或业务异常不进入缓存，后续调用可以重试。缓存值仅为目录短字符串或 `None`。

正式单工作簿、批量版本对比和 M4 Runner 共享应用内同一个 Resolver 实例，因此可复用同一冻结身份的目录事实。服务重启后缓存自然清空。

## 3. 固定延迟验收

命令：

```powershell
py -3 -m app.tools.version_comparison_directory_cache_acceptance_safe `
  --workbooks 55 `
  --delay-seconds 0.005 `
  --output <report-directory>\batch2-directory-cache-acceptance.json
```

本地 Provider 只返回确定目录事实，每次目录调用固定延迟 5ms；它不访问 SVN、不读取业务工作簿、不写数据库或黄金夹具。

| 指标 | 无共享缓存 | 共享缓存 |
|---|---:|---:|
| `list_tree` | 110 | 2 |
| `list_children` | 110 | 2 |
| 目录阶段墙钟 | 1.181s | 0.022s |
| 目录阶段加速 | - | 53.2x |

该结果证明相同冻结身份的重复目录查询已消除，不代表正式端到端会加速 53.2 倍。正式收益仍取决于实际 SVN 命令延迟；未获授权时不得自行访问 SVN 补测。

## 4. 语义与资源回归

五轮离线 Replay：每轮 55 matched / 0 mismatched，结果集合 SHA 均为：

`d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f`

热轮全部完成 P50 为 28.24s，相对批次 1 的 27.49s 约增加 2.7%；峰值工作集 744.4MB，低于批次 1 的 747.1MB。首轮 47.74s 受到同机多个 Python 服务进程竞争影响，单独标记，不用于目录缓存收益判断。离线 Replay 不经过 Resolver 和 SVN，只承担语义、解析计算和资源回归门禁。

临时 `BatchDiffService` 验收：任务 `completed`，55 matched / 0 mismatched，结果 SHA 相同，`temporary_state_removed=true`。该链路同样使用 Replay 本地物化，不测量目录缓存收益。

## 5. 自动化覆盖

- 同 key 串行命中和成功缺失负缓存；
- 八线程 single-flight 只执行一个 loader；
- Provider 异常不缓存且后续可重试；
- 256 项有界 LRU 的通用淘汰行为；
- Revision、URL 和物理路径配置变化不共享事实；
- 完整 Resolver 连续解析两次只执行每侧一次 TABLE/TableCsv 发现；
- 既有目录大小写、CSV 冲突、业务错误和临时目录清理测试保持通过；
- 全量 Replay 结构化结果和 SHA 一致。

## 6. 数据与进程影响

不改变数据库 schema、SQLite 状态机、磁盘缓存格式、JSON 契约、公开 API 或进程模型。不新增线程池；single-flight 只协调调用方已有线程。最坏常驻值为 256 个 key 和短路径字符串，不持有目录树、工作簿或 CSV 字节。

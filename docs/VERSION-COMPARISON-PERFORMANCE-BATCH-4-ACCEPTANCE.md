# 版本对比性能优化批次 4 验收

## 1. 范围

本批次仅复用 `SVNWorkbookDatasetResolver` 为确定 CSV 精确清单而已经解析出的双侧工作簿清单。未修改 core 清单解析器、Excel/CSV 配对、主键、语义 Diff、CSV 并发、缓存或前端行为。

`WorkbookDataset` 新增仅供进程内调用链使用的可选 source/target 清单。真实 `WorkbookDiffService` 显式声明支持该内部能力；单工作簿 API 和默认批量 Runner 仅在双侧清单同时存在时传入。Bound Resolver、Replay、本地工具和测试替身默认不提供清单，继续使用原三参数调用和原解析路径。

## 2. 回退与错误语义

- Resolver 只有在 source 和 target 清单都成功解析时才携带双侧清单；
- 任一侧解析失败时，两侧都不携带，Diff 服务重新解析双侧工作簿；
- `compare_local` 只收到一侧清单时同样完整回退双侧解析；
- 清单解析失败仍由 Diff 服务生成 HTTP 200 业务失败结果，不在 Resolver 层改写错误；
- 不捕获 `TypeError` 后重试，避免真实 Diff 已经执行时发生重复调用；
- 不支持该内部能力的注入式服务继续接收原三个位置参数。

因此本批次不跳过任何校验，也不信任不完整的预解析状态。

## 3. Mock SVN 正式入口验证

现有 Atlas Mock SVN 固定样例通过真实 Resolver、单工作簿 API 和真实 Diff 服务执行。计数结果：

| 阶段 | 清单解析次数 |
|---|---:|
| Resolver | 2，source/target 各一次 |
| Diff 服务 | 0 |

响应仍为 `m2.diff.v1`，方向保持 `source=left`、`target=right`，修改行仍为 273。旧三参数 Diff 替身、Diff 异常及临时目录清理测试同时通过。

## 4. 五轮 Replay 等价路径验收

命令：

```powershell
py -3 -m app.tools.version_comparison_manifest_reuse_acceptance_safe `
  --rounds 5 `
  --output <report-directory>\batch4-manifest-reuse-acceptance.json
```

每轮对 55 项登记 Replay 执行三方比较：旧等价路径、复用路径、黄金结果。旧等价路径包含 Resolver 双侧解析和 Diff 再次双侧解析；复用路径包含 Resolver 双侧解析和 Diff 直接使用该清单。

| 指标 | 结果 |
|---|---:|
| 每轮结果 | 55 matched / 0 mismatched |
| 结果集合 SHA 唯一值 | 1 |
| 结果集合 SHA | `d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f` |
| Resolver 清单解析 | 每轮 110 次 |
| 旧 Diff 清单解析 | 每轮 110 次 |
| 复用 Diff 清单解析 | 每轮 0 次 |
| 旧等价路径 P50 | 49.27s |
| 复用等价路径 P50 | 25.74s |
| 直接节省 P50 | 23.52s |
| 等价计算阶段加速 | 1.914x |
| 峰值工作集 | 799.7MB |

峰值工作集相对批次 1 的 747.1MB 增加约 7.0%，低于本批 10% 门禁。工具同轮执行旧、新两套计算，因此总运行时间和峰值不能当作正式单路径资源值；门禁取更保守的该进程峰值。

该约 23.5 秒是 55 项本地等价计算中消除第二次清单解析的直接证据。它不包含 SVN 目录发现、内容读取、结果存储或前端加载，不能直接宣称正式历史任务端到端获得相同比例收益。未获授权时没有访问正式 SVN。

## 5. 回退路径与自动化覆盖

- 传入完整双侧清单时结果 JSON 字节与原路径完全一致，Diff 清单解析从 2 次降为 0；
- 只传入一侧清单时结果字节一致，Diff 仍解析 2 次；
- 默认批量 Runner 对完整清单使用复用路径，对本地 Dataset 保持原路径；
- 性能计时子类透传内部可选参数；
- Mock SVN API 保持冻结 Revision、精确 CSV、方向、摘要和临时目录清理语义；
- 临时 `BatchDiffService` 回退验收为 `completed`、55 matched / 0 mismatched、SHA 不变，`temporary_state_removed=true`；
- 五轮共物化和清理 275 个工作簿数据集，没有残留 `excel-merge-diff-*` 临时目录。

## 6. 数据、缓存与进程影响

不改变数据库 schema、SQLite 状态机、磁盘或进程内缓存格式、结果 gzip、JSON 契约、公开 HTTP API 或进程/线程模型。清单对象仅随当前 `WorkbookDataset` 生命周期持有，不跨任务缓存；工作簿和 CSV 仍来自同侧、同一冻结 Revision，SVN 保持只读。

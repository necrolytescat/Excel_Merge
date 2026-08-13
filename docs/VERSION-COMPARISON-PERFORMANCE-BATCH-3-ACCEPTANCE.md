# 版本对比性能优化批次 3 验收

## 1. 范围

本批次仅优化 `SVNWorkbookDatasetResolver` 的 CSV 内容读取。未实施清单解析复用、core 解析策略调整、进程内内容缓存治理或前端摘要加载优化。

正式解析路径先对 source 和 target 清单中的精确 CSV 文件名去重，再使用每工作簿一个短生命周期线程池读取。旧串行读取方法仅保留为相同实现版本的性能对照入口，不是第二套正式业务路径。

## 2. 并发与顺序契约

- 单工作簿线程池固定最多 4 个 Provider 调用；现有全局 2 个工作簿并发下，理论 Provider 峰值为 8；
- source 和 target 的精确路径读取任务先全部提交，结果不按 Future 完成顺序解释；
- 结果及异常始终按 `source -> target -> manifest 顺序`归并；
- 精确路径缺失时，每侧至多建立一次大小写目录索引；唯一实际回退路径去重后并行读取；
- 大小写冲突、文件缺失及 Provider 错误继续沿用既有业务错误类型和优先级；
- 异常退出会取消尚未开始的 Future，等待已运行的 Provider 调用结束，并关闭线程池。

Python 线程不能强制终止正在执行的 Provider 调用，因此取消仍依赖 Provider 自身超时后完成释放；这与既有外层任务超时模型一致。

## 3. 固定延迟验收

命令：

```powershell
py -3 -m app.tools.version_comparison_csv_parallel_acceptance_safe `
  --files-per-side 32 `
  --delay-seconds 0.01 `
  --rounds 5 `
  --output <report-directory>\batch3-csv-parallel-acceptance.json
```

本地 Provider 对每次读取施加固定 10ms 延迟，不访问 SVN，不读取业务工作簿，不写数据库或黄金夹具。

| 指标 | 结果 |
|---|---:|
| 每侧文件数 | 32 |
| 串行 P50 | 0.665s |
| 并行 P50 | 0.167s |
| CSV 获取加速 | 3.985x |
| Provider 峰值并发 | 4 |
| 结束后工作线程 | 0 |

20 轮生命周期复测的加速为 3.972x，Provider 峰值并发仍为 4，结束后工作线程为 0。两个工作簿并发专项测试确认峰值大于 4 且不超过 8。

该结果只证明固定延迟 Provider 下的 CSV 获取阶段收益。未获授权时没有访问正式 SVN，不能据此宣称正式端到端获得同等加速。

## 4. 语义与资源回归

五轮离线 Replay 每轮均为 55 matched / 0 mismatched，结果集合 SHA 唯一且保持为：

`d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f`

Replay 墙钟 P50 为 28.92s，峰值工作集为 723.3MB，低于批次 1 的 747.1MB，未超过 15% 资源门禁。离线 Replay 不经过 Resolver 或 Provider，仅承担语义和资源回归门禁。

临时 `BatchDiffService` 验收结果为任务 `completed`、55 matched / 0 mismatched、结果 SHA 相同、首结果 0.20s、全部结果 30.61s，并且 `temporary_state_removed=true`。该链路使用 Replay 本地物化，同样不测量 CSV Provider 获取收益。

## 5. 自动化覆盖

- 固定延迟 Provider 的 CSV 获取阶段至少提升 2 倍；
- 单工作簿 Provider 峰值为 4，两个工作簿并发时峰值不超过 8；
- source 错误优先于更早完成的 target 错误，单侧错误按 manifest 顺序稳定；
- manifest 文件名和大小写回退实际路径均去重；
- 每侧缺失回退只建立一次目录索引；
- 正常、Provider 异常和大小写冲突后均不残留 `m2-csv-read` 工作线程；
- 既有冻结目录缓存、Resolver 业务错误和性能计时适配器保持通过；
- 全量 Replay 结构化结果和结果集合 SHA 保持一致。

## 6. 数据、缓存与进程影响

不改变数据库 schema、SQLite 状态机、磁盘缓存格式、结果 gzip 格式、JSON 契约或公开 API。不新增进程；每个正在解析的工作簿新增一个最多 4 线程的短生命周期线程池，随解析成功或失败同步关闭。

CSV 字节仍只在当前工作簿解析期间持有，不新增常驻内容缓存。Excel 与 CSV 继续来自同侧、同一冻结 Revision；SVN 保持只读，Diff 语义和结果完整性不变。

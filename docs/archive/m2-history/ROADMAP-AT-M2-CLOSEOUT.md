# 产品 Roadmap

## 1. 当前状态

M0.1 与 M1 均已归档。M1 已交付“版本对比”页面、端点注册表、Table Excel 快照、文件级差异候选和只读 Provider；真实 KR FIX 验证结果为两端各 197 个 Excel、53 个文件级差异候选，Table 物理路径已正式绑定为 `Source/table`。M2 单工作簿链路、冻结 Revision 的 SVN 数据适配、M2-07 单机批量运行时和 M2-08 差异结果前端改造均已完成。当前离线 Replay 保持 55/55 matched、0 mismatched；M2 仍需补齐正式任务审计记录和输入完整的最终夹具评审，因此尚不进入 M3。

## 2. 已确定方向

- 页面只允许选择两个已注册、已启用的具体 SVN 端点；
- 快照在点击确认时分别冻结当前 HEAD，Revision 由服务解析并记录；
- M1 读取范围固定为 `Table` 逻辑目录下的全量 `.xlsx`、`.xlsm`、`.xls`；
- M1 不提供日期选择或手工 Revision 输入；可筛选文件级差异候选，但不做 Excel 工作簿、Sheet、单元格解析或 SVN 写操作；
- 端点注册表保存物理路径绑定，页面展示逻辑目录名；
- M2 中 `Table` Excel 与 `TableCsv` CSV 自动绑定到同一端点、同一冻结 Revision，不能分别选择；
- Excel 用于候选筛选、`main` 映射和展示结构，业务值 Diff 以可靠导出的 CSV 为准。

## 3. M0：SVN 只读基座（已归档）

- 本地 FastAPI Web 和健康检查；
- SVN CLI/Mock Provider；
- URL、目录树、日志、内容只读接口；
- 认证缓存复用、错误码和路径安全校验；
- 基础连接配置与区域候选目录预览。

## 4. M1：版本对比快照（已归档）

### M1-01 正式 SVN 拓扑确认

- 核实 KR、TC、JP、BT 真实 FIX 端点 URL；
- 核实每个端点的 `Table` 实际相对路径、大小写和层级；
- 将确认后的物理路径写入端点注册表。

### M1-02 端点注册表

- 支持多个具体 FIX 记录，例如 `KR_FIX_1_1_0`、`KR_FIX_1_0_0`；
- 保存 id、region、track、label、url、logical_scopes、physical_path_filters、enabled；
- 不保存日期或任务 Revision。

### M1-03 版本对比页面

- 左/右端点选择、交换和连接状态；
- 端目录固定为 `Table`，标注全量 Excel；
- 大宽度快照摘要、阶段进度和文件级差异候选清单；
- 确认按钮为“锁定并读取快照”；选择两个不同的启用端点后可执行。

### M1-04 快照接口

- 请求只携带两个端点 ID；
- 每端点只解析一次 HEAD，并以冻结 Revision 递归读取 Table；
- 过滤 `.xlsx/.xlsm/.xls`，返回作者、时间、版本号、大小、缓存引用和失败清单；
- 缓存键为 `(repository, endpoint_url, path, frozen_revision)`；
- 端点不可达时任务整体失败，单文件失败进入失败清单。

### M1 完成标准

用户可以输入并确认两个具体 FIX 分支，得到两份稳定的 Table Excel 快照和文件级差异候选；后续分支变化不影响已冻结 Revision 的复现。真实 KR FIX 验证为两端各 197 个 Excel、53 个候选；M1 不生成工作簿、Sheet 或单元格级 Diff。

## 5. M2：Excel 组织下的 CSV 语义 Diff（当前阶段）

M2 采用“单工作簿数据集先行、SVN 后接入”的顺序。首个验证对象是左右两个 AtlasConfig Excel+CSV 数据集，不直接从 53 个 SVN 候选批量起步。方向固定为 `source=left`、`target=right`，暂不推断 `old/new`。

### M2-00 Web 前端适配

- 调整当前版本对比页的信息架构和大宽度布局；
- 保留 M1 的端点输入、快照摘要和文件级候选；
- 新增单文件 Diff 工作台、Sheet 导航和差异详情骨架；
- 定义 loading、empty、error、ready 状态，不提前实现或伪造语义 Diff。

### M2-01 至 M2-05 单文件语义引擎

- 使用 `tests/excel/left` 和 `tests/excel/right` 中的固定 Excel+CSV 样本；
- 从 Excel `main` 读取 `sheetName` 与 `tbxName` 映射，不比较公式、格式或宏；
- 按 CSV 第 2 行字段名、第 3 行类型、第 4 行范围、第 8 行数据和 `Id/id` 主键计算差异；
- 建立独立于 SVN 的解析模型和 Diff JSON 契约；
- 输出逻辑 Sheet、行和字段级差异；
- 将单文件结果接入 Web 工作台并验证大表展示。

当前 M2-01 至 M2-05 单工作簿链路已完成：固定输出 SHA-256 为 `430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7`；正式工作台已直接消费 `m2.diff.v1`。

### M2-06 至 M2-07 SVN 集成与报告输入

- 复用 M1 已冻结快照和文件级候选，不重建 SVN 流程；
- 从同一端点、同一冻结 Revision 的 `Table` 与 `TableCsv` 读取候选工作簿及其对应 CSV；
- 生成稳定的 HTML/JSON 报告输入；
- 保持 SVN 只读，不写回工作簿或分支。

其中冻结 Revision 的单工作簿 SVN 读取已在 M2-05 阶段 C 完成。`m2.batch.v1` 的单机批量创建、查询、结果读取、取消/重试、持久化、重启恢复、有界并发和失败隔离已在 D2 完成并归入 **M2-07**，用于生成当前冻结快照对的完整报告输入。D2 实施见 `M2-05-STAGE-D2-REVIEW-HANDOFF.md`。

D2 真实试跑发现 29 个业务失败、0 个编排失败，并冻结为五类错误语料。D3 已完成对应分类与引擎加固，包括 TableCsv 文件名唯一大小写匹配和受限的物理第一列主键兜底；不扩展批量调度，不接 Merge/写回。归档交接见 `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md`。

### M2-08 差异结果前端逐项改造（已归档）

- Batch Task、工作簿导航、比对结果标题区和 Sheet 标题与导航已逐项完成并锁定；
- 行与字段差异已改造成左右工作簿对照，支持四向拖拽、空侧占位、选择详情、修改字段循环导航和 TARGET 字符级标红；
- 字段表头显示 `display_name + field_name`，字段视图支持“显示差异 / 显示原表”；
- 正式、Replay、Demo 继续共享渲染契约，未新增第二套前端 Diff JSON；
- `m2.diff.v1`、`m2.batch.v1`、冻结 Revision、SVN 只读和无 Merge/写回边界保持不变；
- 归档记录见 `archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`。

## 6. M3 及以后

- 通用或分布式任务队列、跨节点 Worker、优先级、配额和暂停/恢复；
- 长期历史报告、搜索、导出、通知和任务管理后台；
- 多区域、多轨道和合入归因；
- Excel 值级 Merge 预览与人工确认；
- 经过单独授权和审计后再评估 SVN 写回。

## 7. 明确不做

M1 不扩展为全仓库扫描，不读取 CSV/TBX，不通过日期推断历史状态，不把 Revision 暴露为端点配置，也不执行 `svn commit`、`merge`、`update`、`copy`。M2 批量运行时只处理服务端按 M1 规则重建的候选，并只读取候选工作簿映射到的 `TableCsv` 文件；全链路始终保持 SVN 只读。

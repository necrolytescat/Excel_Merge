# 工程基线

> M1 基线状态：已冻结并归档（2026-08-05）。M1 仅接受阻断性缺陷修复。

## 1. M1 设计基线

M1 是本地 FastAPI + SVN Provider 的只读快照层。业务入口是两个端点 ID，端点配置与一次快照的冻结 Revision 分离。

- 端点注册表：稳定保存多个具体 FIX URL、区域、轨道、标签和 Table 物理路径；
- 快照确认：服务对每个端点读取一次 HEAD，并立即记录具体 Revision；
- 文件范围：绑定的 `Table` 目录递归读取 `.xlsx`、`.xlsm`、`.xls`；
- M1 不解析 Excel 工作簿、Sheet 或单元格；快照哈希只用于筛选文件级差异候选，不执行 SVN 写操作；
- 所有页面和 API 错误使用稳定错误码，禁止泄露凭据和完整内部堆栈。

## 2. 分层结构

```text
Web/API
  ├─ Endpoint registry / snapshot request schema
  ├─ SnapshotService
  │    ├─ endpoint validation
  │    ├─ HEAD freeze
  │    ├─ Table path discovery
  │    ├─ Excel manifest + bounded reads
  │    ├─ file-level difference candidates
  │    └─ failure list / statistics
  └─ SVNProvider
       ├─ MockSVNProvider
       └─ CLISVNProvider (svn info/list/cat only)
```

Provider 不依赖 FastAPI；快照服务不直接调用 subprocess。

## 3. 数据模型

端点记录：

```text
id, region, track, label, url,
logical_scopes=[TABLE],
physical_path_filters={TABLE: relative_path}, enabled
```

快照文件：

```text
path, logical_scope, size, revision, author, date,
content_ref, content_hash, error
```

`revision` 在快照中来自冻结 HEAD 或文件元信息；它不是端点表单输入。

## 4. 路径和扩展名规则

- 相对路径统一为正斜杠；
- 拒绝 `..` 路径穿越；
- 逻辑目录匹配和物理绑定匹配大小写不敏感；
- 只接受 `.xlsx`、`.xlsm`、`.xls`（扩展名比较大小写不敏感）；
- CSV、TBX、其他目录不进入 M1 manifest；
- 页面展示逻辑目录 `Table`，不硬编码物理层级。

## 5. 缓存与并发

二进制内容缓存键：

```text
(repository_uuid, endpoint_url, normalized_path, frozen_revision)
```

内存缓存只保存当前进程中的二进制引用；后续可替换为本地 artifact 文件。文件读取使用有界线程池，单文件失败不掩盖其他文件结果；HEAD、端点 URL 或目录列举失败则任务整体失败。

## 6. API 基线

```text
GET  /api/svn/endpoints
POST /api/svn/endpoints
POST /api/svn/endpoints/{endpoint_id}/discover
POST /api/svn/snapshots
```

快照请求：

```json
{
  "source": {"endpoint_id": "KR_FIX_1_1_0"},
  "target": {"endpoint_id": "KR_FIX_1_0_0"}
}
```

M0 的通用 `tree/log/content` 接口仍可接受历史 Revision 用于底层只读能力，但 M1 版本对比页面不暴露这些输入。

## 7. 只读安全基线

源码和运行路径不得出现 `svn commit`、`svn ci`、`svn merge`、`svn update`、`svn copy` 或其他写操作。URL 不得携带账号和密码；认证继续使用本机 SVN CLI 缓存。

## 8. 测试基线

- 页面契约：无日期、无手工 Revision、只展示 Table 和 Excel；
- 注册表：多具体 FIX 端点、重复 ID、禁用端点和物理路径校验；
- 快照：每端点一次 HEAD、冻结 Revision 后统一读取、大小写路径映射；
- 过滤：只含 `.xlsx/.xlsm/.xls`，排除 CSV、TBX、其他目录；
- 失败：单文件进入失败清单，端点不可达整体失败；
- 缓存：键包含仓库、端点 URL、路径、冻结 Revision，避免跨分支串缓存；
- 只读审计：源码不含 SVN 写操作。

## 9. ADR 索引

- ADR-001：SVN 凭据不进入应用配置；
- ADR-002：Provider 与 Web 分层；
- ADR-003：路径安全和稳定错误码；
- ADR-004：任务结果使用不可变内容引用；
- ADR-005：M0 严格 SVN 只读；
- ADR-006：M1 确认时冻结 HEAD，用户不输入日期/Revision，读取 Table 全量 Excel；
- ADR-007：M2 将 Table Excel 与 TableCsv CSV 绑定到同一端点、同一冻结 Revision。

## 10. M2 稳定 Diff JSON 基线

- 契约版本：`m2.diff.v1`；
- Excel 只读职责：提取 `main.sheetName/tbxName`，`openpyxl` 失败时使用最小 OOXML 兜底；
- 业务比较数据：`TableCsv` CSV，第 2/3/4 条逻辑记录为字段、类型、范围，第 8 条起为数据；
- 匹配规则：`sheetName → Id/id → 字段名` 精确匹配，不使用行号、内容哈希或模糊兜底；
- 方向：`source=left`、`target=right`；
- 规范输出：`.cache/m2/AtlasConfig.diff.json`；
- 固定 SHA-256：`430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7`；
- 回归结果：全量 135 passed。

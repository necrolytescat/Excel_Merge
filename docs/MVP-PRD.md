# MVP PRD：本地 Web 版本对比

> M1 状态：已归档（2026-08-05）。后续新增能力进入 M2。

## 1. 产品目标

为游戏策划、QA 和版本负责人提供 Windows 本地网页工具，用两个已注册 SVN FIX 端点生成稳定的只读快照，供后续 Excel Diff 使用。

典型场景：版本日 21:00 锁定 `FIX1.1.0`，与线上 `fix1.0.0` 做全量 Table Excel 对比。M1 完成快照确认，并输出文件级差异候选，不做工作簿或单元格级 Diff。

## 2. M1 用户流程

1. 用户打开“版本对比”；
2. 选择左侧端点和右侧端点；
3. 页面展示两端 URL、逻辑目录 `Table` 和 Excel 范围；
4. 点击“锁定并读取快照”；
5. 服务分别读取当前 HEAD，解析为具体 Revision 并冻结；
6. 递归读取两个端点 Table 下的 `.xlsx`、`.xlsm`、`.xls`；
7. 返回两个 manifest、文件元信息、统计和失败清单；`03 · DIFF CANDIDATES` 仅显示路径或内容哈希不一致的文件。

页面不提供日期选择或手工 Revision 输入；页面不展示工作簿、Sheet 或单元格级 Diff 结果。

## 3. 端点注册表

```json
{
  "id": "KR_FIX_1_1_0",
  "region": "KR",
  "track": "FIX",
  "label": "FIX1.1.0",
  "url": "https://svn.example/repo/branches/KR-fix-1.1.0",
  "logical_scopes": ["TABLE"],
  "physical_path_filters": {"TABLE": "Resource/Table"},
  "enabled": true
}
```

注册表允许多个具体 FIX 端点。`physical_path_filters` 由拓扑确认流程写入，页面只展示逻辑名 `Table`。

## 4. 请求和响应契约

请求：

```json
{
  "source": {"endpoint_id": "KR_FIX_1_1_0"},
  "target": {"endpoint_id": "KR_FIX_1_0_0"}
}
```

响应核心字段：

```json
{
  "captured_at": "2026-08-04T13:00:00Z",
  "logical_scopes": ["TABLE"],
  "source": {
    "endpoint_id": "KR_FIX_1_1_0",
    "resolved_revision": 26418,
    "physical_path_filters": {"TABLE": "Resource/Table"},
    "files": [
      {
        "path": "Resource/Table/Arena.xlsx",
        "logical_scope": "TABLE",
        "revision": "26418",
        "author": "planner",
        "date": "2026-08-04T10:32:00Z",
        "content_ref": "memory://snapshot/...",
        "content_hash": "..."
      }
    ],
    "stats": {"file_count": 128, "total_size": 5033164, "failed_count": 0}
  },
  "target": {}
}
```

## 5. 业务规则

- 两个端点必须存在且启用；
- 每端点在确认时只解析一次 HEAD；
- 后续列目录、读文件全部使用冻结 Revision；
- 只读取 Table 下的 Excel，路径递归且大小写兼容；
- 其他目录、CSV、TBX 和非 Excel 文件排除；
- 单文件失败进入失败清单；端点不可达导致任务失败；
- 缓存键包含仓库身份、端点 URL、规范化路径和冻结 Revision；
- 不执行任何 SVN 写操作。

## 6. M1 非目标

- Excel 工作簿、Sheet、单元格解析；
- 工作簿、Sheet、单元格级 Diff、报告、Merge 或冲突判断；
- 日期到历史 Revision 的推导；
- SVN commit、merge、update、copy；
- 全仓库目录扫描。

## 7. 后续阶段

M2 先执行 Web 信息架构适配，并用同一工作簿的 old/new 文件样本独立完成 Excel 语义引擎；引擎稳定后，再接入 M1 的二进制快照和文件级候选，生成 Sheet、行和单元格级差异。M3 再讨论任务历史、报告和人工确认后的 Merge。

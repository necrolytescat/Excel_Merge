# M2-05 阶段 D3-B：真实数据兼容性实施结果

> 状态：实施完成；等待 D3-C 真实 Web 回归评审
> 实施日期：2026-08-06
> 规则来源：`docs/M2-05-STAGE-D3-A-COMPATIBILITY-REVIEW.md`
> 自动化结果：`202 passed, 1177 warnings in 8.16s`

## 1. 实施结论

D3-B 已按评审确认的窄规则完成 manifest 与 TableCsv 兼容性加固。未修改
`core/semantic_diff.py`，未扩大主键身份，也未加入行号、首列、任意唯一列、复合键或内容哈希兜底。

| 类别 | 已实施规则 | 保护行为 |
|---|---|---|
| manifest 公式 | openpyxl 使用 `data_only=True`；OOXML 兜底读取公式单元格 `<v>` 缓存 | 不执行公式，不从工作簿或 Sheet 名推导 CSV 名 |
| manifest 范围 | 唯一包含 `sheetName/tbxName/isExport` 的 Excel Table `ref` 是解析边界 | 表外内容不参与；多候选 Table 返回稳定错误 |
| 导出资格 | 只有 `isExport=1` 进入 manifest 和后续 CSV 读取 | `0`、空值和其他值均跳过；导出行缺字段仍失败 |
| 非业务列 | 第 4 行 scope 大小写无关等于 `None` 时排除 | 不参与重复字段、主键、业务行判空和 Diff |
| 注释列 | 显示名非空、代码名/类型为空、scope 为空的列作为窄注释列排除 | 右侧业务字段保留原物理位置；活跃空代码列仍失败 |
| 主键 | 配置候选按 casefold 匹配，必须唯一，输出保留原字段名（如 `ID`） | 多个大小写变体报歧义；没有匹配时保持缺主键错误 |

## 2. 代码与回归覆盖

修改文件：

- `core/workbook_manifest_parser.py`
- `core/table_csv_parser.py`
- `tests/unit/test_workbook_manifest_parser.py`
- `tests/unit/test_table_csv_parser.py`
- `tests/integration/test_atlas_config_diff.py`

固定回归结构由测试代码生成合法的 Excel Table 关系、公式缓存节点和带 BOM 的 CSV，
不复制生产完整文件，也不包含端点、凭据、绝对路径或生成时间。

manifest 覆盖：

- openpyxl 主路径和 OOXML 兜底均读取字符串公式缓存；
- Table 外辅助行被截断；
- 仅 `isExport=1` 进入清单；
- 导出行公式无缓存时返回 `M2_MANIFEST_FIELD_MISSING`，包含行号和缺失字段；
- 多候选 Table 返回清单歧义错误。

CSV 覆盖：

- `scope=None` 在重复字段校验前排除；
- 过滤后仍有活跃同名字段时保持 `M2_CSV_DUPLICATE_FIELD`；
- 中间注释列不导致右侧字段错位；
- 声明类型或活跃 scope 的空代码列保持 `M2_CSV_STRUCTURE_INVALID`；
- 仅非业务列有值的数据记录按业务空行跳过；
- 唯一 `ID` 被接受，`Id/ID` 同时存在时报主键歧义；
- 未配置字段、首列和复合列不被推断为主键。

## 3. 自动化证据

| 范围 | 结果 |
|---|---:|
| D2/D3-A 基线 | `189 passed, 1177 warnings` |
| manifest 单元测试 | `6 passed` |
| CSV 单元测试 | `12 passed` |
| manifest + CSV + semantic + Atlas 定向测试 | `22 passed` |
| 全量测试 | `202 passed, 1177 warnings in 8.16s` |

新增 13 个回归用例；warning 数量未增加。

## 4. Atlas 固定真值

Atlas 规范 JSON SHA-256 从

`430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7`

更新为

`fd15a0f07d490b76bc64a1c406782324caae6de9a08c56cfe12eba1db0777190`。

变化仅来自 9 个 Atlas CSV 的 `scope=None` 字段不再进入字段定义和行 `values`。
以下语义真值未变化：

- 16 个 Sheet 的名称与顺序；
- 7 个 unchanged、9 个 modified、0 个 failed；
- 56 个 source-only、39 个 target-only、273 个 modified 行；
- 375 个 modified fields；
- `TeamConfig`、`TeamStar` 的固定行配对摘要。

集成测试新增断言，禁止任一侧的 `scope=None` 字段再次进入输出。

## 5. 边界与未执行项

本轮未执行 SVN 操作，未创建真实批量任务，未修改批量调度、页面、Merge 或写回。
`MainActivity_FunctionName.csv` 这类 `isExport=1` 且精确文件缺失的源数据缺陷仍应保持
`M2_CSV_MISSING`；D3-B 没有扫描目录或猜测替代文件。

D3-C 尚未开始，因此当前不能声称真实 54 个工作簿的错误基线已经下降。

## 6. D3-C 评审门禁

可以通过 Web 端发起并查看 D3-C 结果，但验收依据必须同时保留任务 ID 和结果 JSON，
不能只按页面汇总数字判断。下一轮批准后应：

1. 使用 D2 相同 source/target 端点和冻结 Revision `26421`；
2. 确认 54/54 项进入终态，编排失败为 0；
3. 对照原 25 个成功项，确认没有退化；
4. 按错误码比较新旧数量，并把每一处减少映射到本报告的具体规则；
5. 确认真正导出文件缺失、无可证明主键和活跃字段歧义仍保持业务失败；
6. 保存新任务证据，再决定是否进入后续阶段。

# M2-08 归档记录：行与字段左右对照与字段视图

## 归档信息

- 归档日期：2026-08-09
- 状态：用户验收通过，已冻结为 M2-08 前端基线
- 起始提交：b962a44897769bc55a88d73851b00c0575a5b48e
- 阶段归属：M2-08，仍属于 M2；本归档不代表整个 M2 完成
- 方向：source=left，target=right
- SVN：只读；归档过程未访问 SVN、未运行新正式任务
- 写回：无 Merge、无 Excel 写回、无 SVN 写操作

## 数据基线

- 夹具：var/m2-fixtures/d3c-6e501824.m2fixture
- Task：6e501824-ac7d-49d4-bd7f-6d7136a958f1
- Source：KR_FIX_KR-Fix-1.0.0.0 @ r26438
- Target：KR_FIX_KR-Fix-1.0.1.0 @ r26438
- SHA-256：bde0ff57c39cf53c9370ab76b5c496f9d2129b06b9336695204b8662c501e296
- 大小：46,210,882 bytes
- 黄金结果：55 个 m2.diff.v1；54 succeeded，1 business_failed

已知 business_failed 来自旧夹具没有保存 MainActivity_FunctionName.csv 大小写变体的
双侧字节。本归档没有访问归档外数据，也没有伪造缺失输入。

## 已交付能力

- 行与字段区域采用类似 Beyond Compare 的左右工作簿对照布局；
- 左侧固定 SOURCE，右侧固定 TARGET，同一业务主键在同一视觉行对齐；
- 支持鼠标四向拖拽，左右纵横滚动同步，状态栏纵向同步；
- 右侧新增和右侧删除使用整行空侧占位，真实侧保留原始逻辑行号；
- 纯修改 Sheet 只显示主键和修改字段；存在新增或删除行时按方案 A 显示全部字段；
- 表头显示 display_name + field_name，字段身份和顺序仍只使用稳定 field_name；
- 单元格支持悬停完整值、左右成对选择和选中详情；
- “修改”状态支持循环定位当前行的修改字段；
- TARGET 修改值仅将变化字符标红，不整格标红；
- 新增“显示差异 / 显示原表”字段视图，原表模式仍只显示差异行但展开全部字段；
- 字段视图在页面内跨工作簿和 Sheet 保持，刷新默认显示差异；
- 大结果继续使用窗口化渲染。

## 锁定边界

- Batch Task、工作簿导航、比对结果标题区、Sheet 标题与导航保持锁定；
- 保持 m2.diff.v1 和 m2.batch.v1，不新增第二套前端 Diff JSON；
- display_name 只是展示元数据，不参与字段匹配、主键、行配对或 Diff 判断；
- 不改变 source/target 方向、冻结 Revision、Core Diff 语义或批量调度；
- 正式结果页不依赖 Demo 假数据。

## 真实 Replay 验收

- Activity/RookieSevenPeriod：差异模式为行号 + Id + PeriodRewards，原表模式展开全部
  6 个业务字段，均保持 3 条差异行；
- 原表模式选中 PeriodPoint 后切回差异模式，同一主键 Id=5 回退到 PeriodRewards；
- AtlasConfig/TeamStar：36 修改、12 删除、14 新增；两种字段视图均按方案 A 显示
  全部 4 个业务字段；
- Loot/Base：2842 条差异行；在 scrollTop=1400、scrollLeft=420 切换字段视图后，
  左右和状态栏位置保持一致，每侧只渲染 25 行；
- 1440x900、1280x800、390x844 下新字段视图控件无重叠、文字完整。

## 自动化证据

- 字段视图针对性契约测试：11 passed；
- 临时工作树排除 5 个依赖缺失 config/settings.json 的模块后：88 passed；
- MAIN 工作树全量 pytest：__MAIN_PYTEST__；
- node --check app/static/compare_results.js：通过；
- git diff --check：通过。

## 主要实现与文档

- app/templates/compare_results.html
- app/static/compare_results.js
- app/static/compare_results_readability.css
- app/static/m2_diff_mapper.js
- core/table_csv_parser.py
- app/schemas/diff.py
- app/services/workbook_diff_service.py
- tests/contract/test_compare_preview.py
- tests/contract/test_diff_web_mapping.py
- docs/M2-08-ROW-FIELD-SIDE-BY-SIDE-PLAN.md

## M2 剩余收尾

本归档只关闭 M2-08 前端改造。整个 M2 在进入 M3 前仍需：

1. 记录可审计正式任务的 Task ID、两侧冻结 Revision、候选数量和终态；
2. 从输入完整的正式任务导出最终夹具，消除当前旧夹具的显式输入缺口并完成评审。

上述事项需要单独授权；不得为了补齐归档而访问 SVN、运行正式任务或更新黄金结果。

## 后续变更规则

本模块后续仅接受用户新确认的问题项。任何新改动必须先基于真实 Replay 说明当前行为、
原因、最小方案、风险和验收用例，不得在本归档范围内顺带扩展。

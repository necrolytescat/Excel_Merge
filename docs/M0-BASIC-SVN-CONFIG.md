# M0.1：SVN 基础配置归档说明

## 目标

本阶段建立本地 Web 页面、SVN 基础配置持久化和只读连接验证能力。首次运行默认使用 Mock Provider，用户可在页面保存 CLI 模式，并在重启后连接真实 SVN。

## 已实现

- FastAPI + Uvicorn 本地 Web 服务；
- 缺少本机 settings.json 时，从可提交模板自动初始化完整配置；
- 基础连接页提供 MOCK/CLI 切换并显示重启生效状态；
- SVN CLI 可用性检测；
- SVN 地址和 Revision 输入；
- 页面“保存地址”写回项目配置；
- POST /api/svn/config 保存地址，GET /api/svn/config 读取当前地址；
- 保存时只更新 svn.server_url，不覆盖其他配置；
- 页面刷新后自动使用已保存地址执行一次只读 SVN info；
- 仓库根地址、UUID、Revision、最近变更信息展示；
- CLI 使用当前 Windows 用户的 SVN 本地认证缓存；
- 非交互 CLI 调用，未缓存凭据时返回认证失败；
- Mock 和 CLI Provider 共用统一模型；
- 认证信息、密码、Token 不进入配置、模型、日志或 API 响应。

## 当前正式配置

实际本机配置位于 config/settings.json，该文件不提交 Git：

- 首次初始化 provider：mock；
- provider：可在页面切换为 mock 或 cli；
- server_url：由当前用户在页面保存；
- credential_source：svn_cli_cache；
- timeout_seconds：30。
完整默认模板保留在 config/settings.m0.example.json，供首次启动自动初始化。

## 页面验收

1. 页面同时显示当前运行模式和已保存模式。
2. 用户可通过开关保存 MOCK 或 CLI 模式。
3. 已保存模式与当前进程不一致时，页面明确提示重启且不执行错误模式的自动探测。
4. 用户可以输入 SVN 地址并点击“保存地址”。
5. 刷新页面后地址仍保留，并在模式一致时自动执行只读连接探测。
6. 测试连接显示仓库信息；失败返回稳定错误码和中文提示。
7. 页面不要求用户输入 SVN 密码，也不保存凭据。

## 暂不纳入 M0.1

- KR/JP/TC/BT 与 Dev/Fix 端点注册表；
- 双端点选择和 Diff 执行按钮；
- 按日期的日志页面和分页；
- 目录树、文件预览页面；
- CSV Diff、报告、Merge、Commit、Update 和其他写操作；
- 本地 SVN 工作区适配。

现有 /api/svn/tree、/api/svn/log、/api/svn/content 只读 API 暂时保留，作为后续阶段基础，不纳入本阶段页面验收。

## 归档证据

- 全量自动化测试：103 passed；
- 正式 SVN CLI 健康检查：provider=cli，svn_cli_available=true；
- KR/DEV svn info 读取成功；
- KR/DEV 的 Source/table 和 Source/TableCsv 目录结构已记录在 docs/SVN-KR-DEV-STRUCTURE.md。
## Revision 边界

系统配置和端点目录不保存 Revision。基础连接测试固定使用 HEAD；历史 Revision、日期和左右端点的版本选择属于后续对比操作参数。后端接口保留 Revision 字段，以兼容后续历史读取。
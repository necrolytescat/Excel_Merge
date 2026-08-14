# M0.1 本地 Web 运行手册

## 1. 安装依赖

~~~powershell
py -3 -m pip install -r requirements.txt
~~~

## 2. 启动页面

在项目根目录执行：

~~~powershell
py -3 -m app.main
~~~

浏览器打开：

~~~text
http://127.0.0.1:5566
~~~

首次启动缺少 `config/settings.json` 时，程序会从可提交模板自动创建完整的本机配置，并以 MOCK 模式启动。

在“系统配置”页可将运行模式切换为 CLI；保存后必须重启服务，确保快照、批处理、监控和计划任务统一使用 CLI Provider。地址配置保存后立即生效，页面刷新时会自动执行一次只读连接探测。

## 3. SVN 认证

真实 SVN 连接使用当前 Windows 用户的 SVN CLI 或 TortoiseSVN 认证缓存，不在配置文件中保存账号和密码。

如果页面提示 SVN_AUTH_FAILED，请先在同一个 Windows 用户下通过 TortoiseSVN 或 SVN CLI 完成一次只读认证。

## 4. 测试

~~~powershell
py -3 -m pytest -q
~~~

M0.1 归档基线为 103 项测试通过。

## 5. M0.1 边界

本阶段只负责基础配置持久化、svn info 连接验证和健康状态展示。

后续阶段再实现端点注册表、按日期日志、双端点选择、Diff、报告、Merge、Commit 和本地工作区。
## Revision 边界

系统配置和端点目录不保存 Revision。基础连接测试固定使用 HEAD；历史 Revision、日期和左右端点的版本选择属于后续对比操作参数。后端接口保留 Revision 字段，以兼容后续历史读取。
# 人工复核边界

当前版本保留既有 `manualReview` 入口并增加授权告知；SQLite schema v4 新增
`review_events`，`POST /api/review` 接受 reportId/claimId/action/source/reason/outcome/
expectedVersion。open → supplement → decide 追加版本，自动结论保持原样；管理员或
reviewer 角色只能处理自己账户拥有的报告。历史和导出附带 reviewHistory。

`POST /api/history/clear` 删除本账户报告与复核事件并清理当前内存；不涉及备份。
启动时 CREATE TABLE IF NOT EXISTS 幂等迁移，未对真实数据库运行。回滚应用可忽略新增表，
不要删除现有自动报告；需删除复核数据时明确处理新增表及外键。前端录入表单未实现。

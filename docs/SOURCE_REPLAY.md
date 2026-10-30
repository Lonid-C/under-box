# 公开来源演示回放

可在部署环境私有目录 `private/source-replay/` 放入经候选人授权整理的来源快照。
文件名为上传原始文件的 SHA-256 加 `.json`，配置 `UNDERBOX_SOURCE_REPLAY_DIR` 可覆盖路径。
仅逐字节一致的文件使用该快照；改名不影响匹配，修改内容后走普通流水线。
代码库不包含候选人简历、联系方式、快照或指纹。

快照格式：`version: 1`、`inputSha256`、ISO 时间 `cachedAt`、`profile`（ResumeProfile）、
`report`（Report 模型）。快照来源、摘录、页码、名单初稿状态和未证实内容由整理者逐项复核。
缺失或损坏的快照回退普通核验，不按文件名或姓名猜测。

上传后仍可逐条勾选。界面、进度日志、JSON/Markdown 导出和问答材料均带
“演示 · 公开来源缓存”及整理时间；加载动画仅呈现回放节奏，不计为实时搜索调用。
预置的待澄清、部分证实和未找到记录状态原样保留。未勾选条目列入未核验清单。
问答继续读取当前报告，用户选择联网问答时仍执行真实网络查询。

验证：在 `open_box` 目录执行 `python -m unittest tests.test_source_replay tests.test_institution_context`。

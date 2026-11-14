# DeepSeek 接入与回滚

主站通过服务器端 `DEEPSEEK_API_KEY` 调用 DeepSeek，密钥不会发给浏览器。

- 文件上传：PDF、DOC、DOCX、TXT、MD 进入 Files API；文本层不足和 DOC 使用 OCR。原始文件在上传入口做本机检查，Files 对象设置 1 小时过期，并在处理完成或出错时请求删除。删除请求失败时依赖到期清理。
- 陈述检验：DeepSeek Search 是通用搜索主服务；仅服务故障时使用账号配置的智谱或 Brave 备用。普通网页优先使用 Fetch，名单附件和需要保留图片链接的官方页面沿用原读取器。原有来源分级、身份与逐项判定仍由规则执行。
- Boxy：默认只结合报告；用户开启联网后可使用 DeepSeek 网页工具并查看返回的来源。语音输入录制后转写到输入框，用户确认后发送；回答可分段朗读。
- 计量：分别记录模型 tokens、搜索和读页次数、文件上传次数、OCR 页数、语音识别秒数与合成输入字符。工具价格未配置时不估算金额，以供应商账单为准。免费与失败调用以接口返回为准，不能据记录推算精确账单。

## 配置

后台 API 管理可选择搜索供应商、文件解析器、网页读取和联网问答模式。运行期对应变量：

- `SEARCH_PROVIDER=deepseek`：默认 DeepSeek；`zhipu`、`brave` 可手动切换。
- `UNDERBOX_DOCUMENT_PARSER=deepseek`：默认上传原件；`local` 切回原本地解析，旧版 DOC 不可用。
- `DEEPSEEK_FETCH_ENABLED=1`：设为 `0` 回到原网页读取器。
- `DEEPSEEK_QA_NATIVE=1`：设为 `0` 使用原问答检索流程。

管理员保存设置会重启工作进程。语音和文件处理需有效 DeepSeek 密钥与可用额度。音频只在请求内存中处理。

## 隔离验证

测试应使用虚构样例或已授权的材料，避免将真实简历、报告和音频加入 Git。

```sh
cd /opt/underbox-repo/open_box
/opt/underbox/.venv/bin/python -m unittest \
  tests.test_deepseek_document tests.test_deepseek_search tests.test_deepseek_fetch \
  tests.test_search_recall tests.test_official_search tests.test_institution_context \
  tests.test_document_gate tests.test_source_replay

cd /opt/underbox-repo
/opt/underbox/.venv/bin/python -m unittest discover -s underbox/tests -p 'test_*.py'
node underbox/tests/test_boxy_drag.cjs
node underbox/tests/test_ink_stream.cjs
```

部署时保留上一版本文件和数据库备份。先验证隔离服务，再同步对应文件、执行数据库迁移并重启服务；出现问题时恢复文件备份并重启，或使用上述开关回切。

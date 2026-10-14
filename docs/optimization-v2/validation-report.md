# Validation report

## 2026-10-15 修复后本地全量回归（真实运行）

环境：临时 venv（Python 3.12.13）+ `requirements.txt` 全部依赖（含新补的 `pdfminer.six`）；
Node v26 运行两个 `.cjs`；Playwright 未安装，浏览器断言未执行。

```sh
venv/bin/python scripts/regression.py --node "$(which node)"
```

| 套件 | 结果 | 说明 |
|---|---|---|
| core | **544/544**；新失败 0 | 含新增 element-proofs/provenance/handoff/retrieval/safe-http 用例 |
| service | **42/42**；新失败 0 | 含 prepared ownership / request limits / review / chat providers |
| legacy | 29/33；已知旧失败 4；新失败 0 | 残留 03/08/30/31，签名与原基线一致 |
| test_manual_review_frontend.cjs | passed | |
| test_usage_frontend.cjs | passed | |

总计 619 项、615 通过；退出码 2（`known_failures`）——**不是全通过**，4 项旧失败见 REGRESSION.md。
修复前同环境 49 项新失败（根因与逐项修复记录见
`outputs/regression/20261015-011213-574291/analysis.md` 与 `20261015-004043-043625/analysis.md`）。

## 契约迁移（均有新旧输入/期望对照，未删用例、未放宽安全约束）

- legacy 02：存档结论保留 + 新规则重算失败保守（旧材料不得重算出 ok），`KNOWN` 签名移除并留档；
- legacy 06/13/20/21/29：按"未验证来源不合并 / 矛盾须逐字 / 未配置明确报错 / 自提供链接不加身份分 /
  故障降级并标记未完成"迁移；
- core：`mk_ev`、`evidence`、`_roster` 等夹具统一绑定 provenance；协作与 GitHub 用例补
  `ElementProof`/provenance；出网用例注入假 DNS 解析器（`tests/http_kit.py`）；
- `pdfminer.six` 纳入必需依赖（此前缺失时 PDF 隐藏文字检测静默失效）。

## 仍未执行

- 真实浏览器（桌面/900px/390px）操作与视觉检查——Playwright 未安装；
- `open_box/bench` 离线重放与指标（脚本与 10 条合成样本已就绪，未运行）；
- 真实模型/搜索供应商、华为云、线上部署——未授权，未执行。

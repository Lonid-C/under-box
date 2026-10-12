# under-box

履历核验系统。**核验的是陈述，不是人**——不给候选人打分、不做排名、不给录用建议。
每条结论都带出处与来源等级，查不到就如实说"未找到公开记录"。

> 设计立场：**判定归规则，理解归模型。** 模型只负责读懂简历、逐字摘录证据；
> "这条算不算被证明了"永远由 `open_box/app/judge.py` 里的规则回答，模型不参与。

## 三个部分

```
open_box/            规则层（Python，纯规则）
  app/parse.py         解析 PDF + 隐藏文字/提示词注入检测
  app/split.py         陈述拆分（模型）
  app/profile.py       画像归纳：身份/行业/层级/技能（模型）
  app/strategy.py      检索策略引擎：档案匹配、查询展开、预算裁剪（规则）
  app/collect.py       检索与页面回读（含学校域动态发现、GitHub 一等锚点）
  app/judge.py         来源分级、身份判定、状态判定（规则）
  data/strategies.json 策略档案：加一种简历类型 = 加一个档案，不改代码
  tests/test_rules.py  33 项旧验收（完整回归入口见 scripts/regression.py）

underbox/            界面层（单页 + 本地服务）
  index.html           落地页 → 拖入简历 → FLIP 动画切到主界面
  serve.py             本地服务：流式核验、流式问答、检索计划、报告导出
  start.sh             一键启动

dsh-resume-verify/   DeepSeek Harness 插件（把规则层暴露成模型可调用的工具）
  src/index.ts         6 个工具 + session 事件
  python/resume_rules.py  Python 桥（JSON stdio）

search-strategy/     检索策略的设计文档与对照脚本
  DESIGN.md            调研 → 设计 → 现状（含踩过的坑与实测数据）
```

## 怎么跑

需要 Python 3.11+、Node 22+（插件用），以及两个 key：

| 变量 | 用途 | 放哪 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 拆陈述、画像、澄清问题、问答 | `open_box/.env`（已 gitignore） |
| `SEARCH_API_KEY` | 检索证据（默认智谱 web_search） | 同上 |

```bash
python3 -m pip install -r requirements.txt
cp open_box/.env.example open_box/.env     # 填入两个 key
cd underbox && ./start.sh                  # 打开 http://127.0.0.1:8787/
```

拖一份 PDF 进去，约 3–10 分钟出结果。左下角状态窗口会实时显示流水线的**真实日志**。

每次修改后跑完整回归（不需要 key）：

```bash
python3 scripts/regression.py
# 或：make test PY=/path/to/python NODE=/path/to/node
```

覆盖当前规则、账户/历史/导出、SSE 和前端检查，并额外运行旧版 33 项验收。检查日志、已知失败及浏览器/部署验证要求见 [REGRESSION.md](REGRESSION.md)；退出码 2 表示仍有已知旧失败，不能当作全部通过。

## 为什么是这套设计

- **判定不用模型。** 有开源实现让模型给简历打分，同一份简历跑 100 次分数在 66–99 之间跳，
  连 `temperature=0` 都稳不住。判定必须可复现，所以它是纯规则。
- **只核验可以证伪的陈述。** 简历原文逐字保留，拆成"要素"（如 `职务=部长`、`起止时间`），
  逐项去公开来源里找。**证明了组织不等于证明了职务**——所以 `部分证实` 是一个独立的正常状态。
- **查不到不等于不实。** `未找到公开记录` 是中性状态（灰色），不是负面信号；
  公网痕迹少不是污点。差异也不等于造假（可能是后续接任或口径不同），所以标 `待澄清` 而非红色。
- **宁可少找，不认错人。** 同名但学校/专业不符的记录不计入本人，标 `身份未确认` 转人工。
- **公众号只走合规通路。** 微信未开放文章检索接口，只用搜索引擎已收录的部分，
  或候选人自提供的链接；不做绕行抓取。

## 不在仓库里的东西

- `open_box/.env`（两个 key）
- 账户 SQLite 数据库、会话、账户专用密钥与 `users/` 目录
- `outputs/` 中的本地测试结果、部署包及服务器备份
- 真实简历样例与真实核验报告——**含个人隐私，只在本地**
- `search-strategy/probe_*.py` 两个一次性调查脚本（硬编码了某份真实简历的路径与姓名）

`sync-open-box.sh` 用于把本地开发中的 `open_box` 同步进本仓库（自动排除上述内容）。

## 已实现与未实现

**已实现**：解析 + 注入检测 → 画像 → 拆陈述 → 策略引擎生成查询 →
检索 + 整页回读 → 逐要素判定 → 澄清问题；界面上的流式核验状态窗、流式问答、
来源分级面板、逐条检索计划、报告导出；搜索结果统一排序与跨查询分配读取预算、
不同域页面并发回读（`COLLECT_WORKERS` 默认 3）；dsh 插件 6 工具。

核验按十三类专项 agent 分工，使用受类别约束的数据源与带出处的协作线索；学历学段、年份、人员身份、奖项及第三方来源分别核对。实习提供企查查公司入口和可取得的公开电话，保持“待人工背调”，不自动证明本人实习。项目只有原文明示 GitHub 材料时才启动仓库核验。

账户网关支持角色、配额、管理台 API 设置及账户专用搜索密钥；成功报告按账户持久化最近十次，可回看与导出。“退出此次查询”清空当前日志和进度、保留登录；登出后须重新认证才可读取历史。单机直接启动与账户网关模式的配置见 [账户与历史说明](underbox/ACCOUNTS.md)。

专项职责、数据源与协作边界见 [指导老师建议的落地](open_box/docs/ADVISOR_ARCHITECTURE_2026-10-12.md)，实习人工背调见 [分类核验说明](open_box/docs/VERIFICATION_AGENTS_2026-10-12.md)。公开登记接口、本地目录与报告数据库各有用途；本项目没有学信网或企业内部人事库的自动访问权限。

**仍待处理**：覆盖度自检（`app/coverage.py`）未实现；完整回归中仍有五项旧验收差异，见 [REGRESSION.md](REGRESSION.md)。

细节与设计取舍见 `search-strategy/DESIGN.md`，各部分的踩坑记录在各自的 README 里。

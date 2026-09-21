# 履历核验 Demo

把一份简历拆成一条条可核验的陈述，对每条给出**带出处的结论**——来源链接、原文片段、
证明了什么、还没证明什么；信息不足时老实说"未找到公开记录"。

```bash
make demo     # 离线演示，不需要任何 API key，打开 http://127.0.0.1:8000/
make test     # 验收清单（19 项）
```

接 DeepSeek 跑真实核验：

```bash
cp .env.example .env
export DEEPSEEK_API_KEY=sk-你的key SEARCH_API_KEY=你的搜索key
make llm-check       # 验 LLM 的 key / 端点 / 模型名
make search-check    # 验搜索的 key / 端点 / 引擎名，并确认 site 限定真的生效
MODE=live python -m app.cli verify samples/resume_lin.pdf -o out/report.json
```

演示时先点第 5 条（科技部部长 / 副部长），再看第 7、8、9 条——
这四条展示的正是"官方发布 ≠ 本人经历为真"和"证明一条 ≠ 证明全部"。

---

## 交付说明（按构建说明第 12 节）

### 1. 哪些数据源真正接通了

**LLM 已按 DeepSeek 写实，但没在真实端点上验证过；搜索还没选供应商。**

| 层 | 现状 |
| --- | --- |
| 解析（PDF / DOCX / Markdown） | ✅ **真的在跑**。PDF 走 pdfminer.six（装了 PyMuPDF 会优先用），DOCX 走 python-docx |
| 隐藏文字与提示词注入检测 | ✅ **真的在跑**。极小字号、白底白字、画到页面外、注入模式四类都能抓到并从送入模型的内容中剔除 |
| 判定层（身份 / 来源分级 / 去重 / 状态） | ✅ **真的在跑**，纯规则、无模型参与、可复现 |
| 报告页与报告 JSON | ✅ **真的在跑** |
| LLM（拆分 / 证据提取 / 澄清问题） | 🟡 **已按 DeepSeek 写实**：JSON Output 模式、429/5xx 退避重试、401/402/422 立即报错、空内容补一次。报文格式用一个复刻 DeepSeek 应答的本地服务器验穿了（验收第 15–19 项）。**但没打过真实的 api.deepseek.com**——本次构建环境的出口策略拦截了该域名（代理返回 403），需要你在本机 `make llm-check` 验一次 |
| 搜索引擎 | 🟡 **已按智谱 Web Search 写实**（`search_domain_filter` 走一等参数）。同样没打过真实端点，用 `make search-check` 验。换别家改 `SEARCH_PROVIDER` 或在 `_parse` 里改字段映射 |
| Crossref / OpenAlex / ORCID / GitHub / 专利 | ⬜ 未接。`plan.py` 已按类别生成了对应查询（`kind=crossref/github/patent`），`collect.py` 目前统一走通用搜索路径 |
| 学信网 | ⬜ **按设计不接**。学历一律 `none` + 授权补证，不做自动化 |
| LinkedIn 等职业平台 | ⬜ **按设计不接**（用户协议限制自动化访问） |
| 微信公众号 | ⬜ 只接受"搜索引擎已收录的链接"或候选人自己提供的链接，不使用 cookie／抓包／代理池／搜狗绕行 |

#### DeepSeek 接入细节

端点与模型名核对自 `api-docs.deepseek.com`（2026-09）：

| 项 | 值 |
| --- | --- |
| 端点 | `https://api.deepseek.com/chat/completions` |
| 默认模型 | `deepseek-flash`（官方推荐，当前由 **DeepSeek-V4.1-Flash** 承接） |
| 备选模型 | `deepseek-v4-pro` |
| 结构化输出 | `response_format={"type":"json_object"}`，且提示词里必须出现 `json` 字样——客户端会自动补 |

⚠️ **别把底层模型版本当成调用名。** 底层跑的确实是 DeepSeek-V4.1-Flash，但官方没有放出
`deepseek-v4.1-flash` 这样的 API model 字符串——`model` 字段要填 `deepseek-flash`，
它会跟着官方升级自动指向最新的 Flash，写死版本号反而会在下次换代时失效。

`deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 是已退役的旧名，仍被接受、同样由
V4.1-Flash 承接并按 Flash 价计费。`deepseek-chat` / `deepseek-reasoner` 是更早的历史名称，
已不在当前列表里。

几个为真实调用做的取舍：`temperature=0`（核验场景必须可复现）；页面正文送模型前截到
12000 字（学校通知页常带整站导航，不截白烧 token）；DeepSeek 官方提示 JSON 模式偶发
返回空内容，客户端会带提示补一次再放弃。

换别家供应商只改环境变量，业务代码不认识任何一家：

```bash
export LLM_PROVIDER=openai-compatible LLM_ENDPOINT=... LLM_MODEL=... LLM_API_KEY=...
```

#### 搜索选型：为什么是智谱

这个场景要搜的是中文的 `*.edu.cn` 公示通知页，而且 `plan.py` 重度依赖按域名限定。
按这两条筛下来：

| 方案 | 结论 |
| --- | --- |
| **智谱 Web Search**（选定） | 中文索引对 edu.cn 深层页覆盖好；`search_domain_filter` 是**一等参数**，正好对上 `search(query, site=)`，不用把 `site:` 拼进查询串；引擎可切：`search_std` 0.01 元/次、`search_pro` 0.03、`search_pro_sogou` / `search_pro_quark` 0.05 |
| Bing Web Search API | **已于 2025-08-11 退役**，不要再选 |
| 博查 Bocha | 同为中文 AI 搜索，返回字段干净（含 `datePublished`），值得和智谱 A/B，但价格未公开 |
| Serper（Google） | $0.30/千次，最便宜，支持完整 Google 语法，适合论文/开源/英文内容；中文高校深层公示页覆盖弱于中文引擎 |

成本量级：一份简历最多 11 条 × 6 次检索 = 66 次。`search_pro` 约 2 元/份，
`search_std` 约 0.7 元/份。建议先用 `search_std` 跑通，覆盖率不够再升 `search_pro`。

#### 关于公众号

**结论：微信公众号文章的检索，目前没有合规的 API 通路。** 查证如下：

- 微信官方「搜一搜」接口是**单向推送**——只能把自己的内容提交给微信索引，不能检索
  别人的公众号文章。
- 搜狗微信搜索（weixin.sogou.com）只有网站，**没有对外开放的官方检索 API**。围绕它的
  生态全是爬虫（`WechatSogou` 这类项目的自我描述就是"基于搜狗微信搜索的公众号爬虫接口"），
  属于本项目明令禁止的"搜狗绕行"。
- 第三方转售的"公众号文章搜索 API"几乎都是爬虫封装。用它等于把禁止的访问方式外包出去，
  合规风险没有减少，只是转移了，而举证责任仍在使用方。
- 智谱 `search_pro_sogou` 是官方授权集成，但文档写明覆盖的是"腾讯生态（新闻/企鹅号）和
  知乎内容"，**没有背书包含公众号文章**。

所以实现只走两条路，都在代码里：

1. **通用搜索限定 `site:mp.weixin.qq.com`** —— 只捞搜索引擎已经收录的那部分。
   `plan.py` 对学生工作／校内荣誉／奖学金／竞赛四类会自动补这条查询。
2. **候选人自己提供的链接** —— `claim.entities["provided_urls"]`，优先读取、**不占检索
   预算**，并由规则（不是模型）补上 `候选人自提供该链接` 身份信号（+2 分）。

公众号被通用搜索收录的比例本就不高，所以第 7 节那句「拿不到就标 `none`」会经常触发。
**这是这个产品的真实上限，应该在产品里讲清楚，而不是用技术手段硬突破。** 对招聘流程
来说，第 2 条其实更自然：与其猜，不如直接问候选人要那份换届公告的链接。

### 2. 哪些还是 mock

- **`mock` 模式（默认）下的整份报告**：`fixtures/report_lin.json` 里 11 条陈述的
  候选人、学校、组织、赛事、期刊、仓库、链接**全部虚构**，域名用 RFC 6761/2606
  保留的 `.example`，不指向任何真实站点。报告页顶部有「候选人与来源均为虚构」标注。
- **不要把 mock 的结果说成真实核验结果。**
- fixture 不是手写死的：`fixtures/build_fixture.py` 把虚构证据喂进 `app/judge.py`
  的同一套规则算出状态，验收第 2 项会再用规则重算一遍比对，两边不会各说各话。

### 3. 检索覆盖率在 fixture 上的实测值

`python -m app.cli stats` 现算现报（下面是本次实测）：

| 指标 | 11 条陈述上的实测 |
| --- | --- |
| 检索到公开来源 | 8/11（73%） |
| 来源可关联到本人 | 7/11（64%） |
| 要素被全部证明（已证实） | 4/11（36%） |
| 要素级覆盖率（20/38 个要素） | 53% |
| 需人工复核 | 2/11（18%） |

`make test` 现在是 23 项：第 11 节的 8 项 + PDF 隐藏文字、来源分级、身份阈值、要素分别
证明，DeepSeek 接入 5 项，搜索接入与公众号合规路径 4 项（含一条防回归：`User-Agent`
必须是 latin-1 可编码的——HTTP 头不接受中文，写了会让**每一个**外部请求都失败，而且
异常会被重试循环吞掉，表现为"搜索永远返回空"）。

> 覆盖率只反映公开信息的多少，不代表候选人的可信程度。以上为虚构 fixture 上的
> 实测值，换成真实简历不具备参考性。

---

## 目录

```
app/     schema 数据模型 · parse 解析与风险检测 · split 拆分 · plan 检索计划
         collect 检索与提取 · judge 判定规则 · questions 澄清问题
         llm/search 供应商适配 · pipeline 编排 · main HTTP · cli 命令行
web/     report.html / report.css  报告页
fixtures/ resume_lin.md 虚构简历 · report_lin.json 虚构报告 · build_fixture.py 生成器
samples/  resume_lin.pdf 带隐藏文字的 PDF 样本 · make_sample_pdf.py 生成器
data/     schools.json 学校域名表（20 所真实 + 2 所虚构）
tests/    test_rules.py 验收清单
```

## 设计上几个不显眼但关键的地方

**LLM 只做理解和提取，所有判定归规则。** `judge.py` 里没有任何模型调用；
模型给出的 `identity_score` 会被规则重算覆盖，`supports` 里不在 `elements` 中的
自造要素会被丢弃。结果因此可复现、可测试、可解释。

**要素是分开证明的。** "2024.03—2025.03 校学生会科技部 部长"拆成任职组织、职务、
起止时间三个要素；换届公告证明了前两个，第三个进 `unproved`。证明了"任职组织"
不等于证明了"职务"。

**同名一票否决。** 只要有一条 `identity_conflicts`，无论身份信号堆多少，一律
`who`。宁可标"身份未确认"让人工去看，也不能把同名他人的记录算到候选人头上。

**转载不构成多重证明。** `origin_url` 相同的证据去重后只计一次，去重时保留等级
最高的那条。三篇转载堆不出一个 `ok`。

**不给候选人打分。** 报告里没有总分、真实性百分比、评级、排名或录用建议，也不判断
简历是否由 AI 撰写。`identity_score` 是"这条记录是不是本人"的身份关联度，只存在于
证据层，不是候选人的分数。未找到和身份未确认一律用中性灰，不用红色——避免把公网
痕迹少的候选人画成有污点。

## 几处需要说明的实现选择

0. **本环境没法访问 api.deepseek.com**（出口策略拦截，代理返回 403），所以 DeepSeek 客户端
   是对着一个复刻其请求校验与响应形状的本地服务器验证的：鉴权头、`response_format`、
   提示词 json 字样、空内容重试、429 重试、401/402 不重试，逐项都有用例。真实端点请用
   `make llm-check` 验一次——如果那一步过了，`verify` 就能跑。
1. **本环境装不上 FastAPI/uvicorn/PyMuPDF/trafilatura/pytest**（PyPI 被出口策略拒绝），
   因此：HTTP 服务用 Starlette（`app/main.py` 检测到 FastAPI 会优先用它，两者都没有时
   还有 stdlib `http.server` 兜底）；PDF 用 pdfminer.six；网页正文用 bs4；测试自带
   runner（`tests/test_rules.py` 同时兼容 pytest 收集）。接口与构建说明一致，装上原定
   依赖后无需改代码。
2. **预算耗尽时的状态**：构建说明第 6.3 节写"超出即置 `search_exhausted=true`、
   `status=none`"。实现为「预算耗尽 → `search_exhausted=true`，状态仍由第 6.7 节的
   `decide()` 决定」——检索被截断且没拿到证据时自然就是 `none`（验收第 8 项覆盖这个
   场景）；但若在耗尽前已经拿到 A 级公示，丢掉它反而不合理，也与 6.7 节冲突。
3. **`needs_human` 只看去重后的证据**：被当作转载丢弃的副本对结论没有贡献，不应该把
   一条干净的 A 级公示拖进人工队列。
4. **第 8 条（论文作者位次）判 `part` 而不是 `ask`**：Crossref 作者列表不记录共同一作
   标注，"列于第二位"不足以认定简历表述有误，所以进 `unproved` 并附原因，由澄清问题
   去问；第 5 条（换届公告明写副部长）才是 `ask`。差异写在 `unproved` 里，不是含糊过去。
5. **可访问性的一处已知欠缺**：构建说明指定小标签用 `--faint`（#8B867B），在
   `--paper`（#F4F1EA）上对比度约 3.2:1，低于 WCAG 的 4.5:1。正文与次要文字
   （`--ink` 约 15:1、`--muted` 约 5.0:1）都达标。若要全量达标，把 `--faint`
   调到 #6F6A5F 左右即可，其余设计不受影响。

## 明确不做

不给候选人整体打分或排名；不判断简历是否由 AI 撰写；不抓 LinkedIn 等职业平台；
不用 cookie／抓包／代理池／验证码破解获取微信公众号内容；不做人脸比对、社交媒体
行为分析或私人生活调查；不主动联系候选人的前雇主、老师或证明人。

所有外部请求都带标明用途与联系方式的 `User-Agent`，遵守 `robots.txt`，
单域名限流 1 次/秒，失败重试不超过 2 次。

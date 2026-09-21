# dsh-resume-verify

把 open_box（履历核验 demo）套进 **DeepSeek Harness**（`deepseek-ai/deepseek-harness`，CLI 叫 `dsh`）的插件。

**命名**：内核（规则层 + 工具）沿用 **open_box**；界面叫 **underbox**。
两个名字是一对——open box 在里面按规则判，under box 把它摊开给人看。

本插件只负责**规则层 + 数据契约**，界面由 underbox 实现（§4 是它的施工图）。

> underbox 已有可点开的静态原型：`../underbox/index.html`（视觉与布局决定见 `../underbox/README.md`）。

---

## 1. 架构映射：套进去之后，哪些留、哪些丢

dsh 的立场是 **Agent = 模型 + Harness**，harness 负责 ReAct 循环、工具调用、会话日志、沙箱与权限。
所以 open_box 里"自己造 harness"的那几层可以整块丢掉：

| open_box 原来 | 套进 dsh 之后 | 为什么 |
| --- | --- | --- |
| `app/llm.py` 自带 DeepSeek 客户端 | **丢弃** | 模型与凭据由 harness 提供，插件不该再认识任何一家 API |
| `app/pipeline.py` 硬编码六步顺序 | **丢弃** | 顺序改由 agent 的 ReAct 循环决定，它可以自己重试、追加检索 |
| `app/main.py` + `web/` 自带前端 | **丢弃** | 改走 session 事件，由 underbox 渲染 |
| `app/split.py` 一次性 prompt 拆陈述 | **丢弃** | 让 agent 自己拆，它能边拆边看上下文，比单次 prompt 强 |
| `app/questions.py` 生成澄清问题 | **丢弃** | 本来就是模型该干的活 |
| `app/judge.py` 判定规则 | **保留** | 纯规则、无模型、可复现——这是最该留的一层 |
| `app/parse.py` 解析与风险检测 | **保留** | 隐藏文字与提示词注入检测必须在内核做，不能交给模型 |
| `app/plan.py` 检索计划 | **保留** | 类别→查询的映射是领域知识，不是模型的活 |

换句话说：**原有的产品原则没变，只是执行者换了。**
open_box 自己的 README 写着"LLM 只做理解和提取，所有判定归规则"——
在 dsh 里这句话变成"理解归 harness 的模型，判定归插件工具"，是同一条原则的架构化版本。

```
dsh agent（模型）
  │  ① resume_ingest              解析 + 隐藏文字/注入检测        ← 规则
  │  ② resume_validate_claim      校验你拆出来的陈述              ← 规则
  │  ③ resume_plan_queries        生成检索查询                   ← 规则
  │  ④ （用 harness 的 web 工具检索、读页、逐字摘录）             ← 模型
  │  ⑤ resume_judge_claim         身份/来源分级/去重/状态判定     ← 全部规则
  └  ⑥ resume_summary             覆盖率汇总 + 边界声明           ← 规则
```

**open_box 一行都不用改。** 规则层的实现是 `python/resume_rules.py`，它通过 `PYTHONPATH`
导入 open_box 的 `app.*` 模块；插件只是给这层套了个工具外壳。

---

## 2. 快速开始

### 2.1 准备一个装了 open_box 依赖的 Python

规则层要 `pydantic`（PDF 解析还要 `pdfminer.six`）。

```sh
cd /path/to/open_box
python3 -m venv .venv && .venv/bin/pip install pydantic pdfminer.six python-docx beautifulsoup4 lxml
```

把 `.venv/bin/python` 的绝对路径填进 `cordis.yml` 的 `pythonBin`。
**不要用系统 `python3`**，除非它确实装过。

### 2.2 拿到 harness

源码方式：

```sh
git clone https://github.com/deepseek-ai/deepseek-harness.git
cd deepseek-harness && pnpm install
```

npm 方式（不需要源码，也能用）：

```sh
npm install @deepseek-ai/dsh
DSH_HOME=/你的/dsh-home npx dsh --profile web --dump-default-config > /dev/null   # 初始化 profile
```

### 2.3 装插件——**这一步有坑，务必用脚本**

dsh 用绝对路径加载插件时，模块解析的起点是**插件文件自己所在的目录**。
插件 import 的 `@deepseek-ai/dsh-tools`、`@deepseek-ai/cordis` 只存在于 harness 的模块树里，
所以**插件不能随便放**——放在 `~/Desktop` 这类地方会让整个 boot 失败：

```
Error: dsh: plugin tree failed to load: ... Cannot find package '@deepseek-ai/dsh-tools'
```

（这个失败是致命的，不是"插件没生效"，而是 dsh 完全起不来。官方教程之所以看起来"随便放都行"，
是因为它的 `scratch-plugin/` 就在 harness 仓库根目录里。）

三个能用的放法，任选其一：

| | 放哪 | 适合 |
| --- | --- | --- |
| **A** | harness 源码仓库内，如 `<repo>/scratch-plugin/dsh-resume-verify/` | 你已经 clone 了源码 |
| **B** | `$DSH_HOME/profiles/resume-verify/` | npm 装的 dsh，不需要 pnpm ← **已实测可用** |
| **C** | `dsh plugin --profile web add file:/绝对路径/dsh-resume-verify` | 装成正式 bundle，**需要 pnpm** |

最省事：

```sh
cd dsh-resume-verify
OPEN_BOX_ROOT=/path/to/open_box \
PYTHON_BIN=/path/to/open_box/.venv/bin/python \
DSH_HOME=/你的/dsh-home \
./install.sh
```

脚本会按方案 B 落盘、按实际路径生成 patch，并顺手检查 `pydantic` 装没装。

### 2.4 起

```sh
DSH_HOME=/你的/dsh-home dsh web --patch /你的/dsh-home/profiles/resume-verify/cordis.yml
```

启动后**第一行**应该能看到装配结果：

```
[resume-verify] 已注册 6 个工具：resume_ingest、resume_validate_claim、resume_plan_queries、
resume_classify_tier、resume_judge_claim、resume_summary　openBoxRoot=…　pythonBin=…
```

看不到这行就是没装上。打开 `http://127.0.0.1:3080`，输入：

> 核验 /Users/a1234/Desktop/open_box/samples/resume_lin.pdf 这份简历

模型会自己走完 ingest → 拆陈述 → plan → 检索 → judge → summary。

---

## 3. 六个工具

| 工具 | 干什么 | 谁在算 |
| --- | --- | --- |
| `resume_ingest` | 解析 PDF/DOCX/MD/TXT，检出极小字号、白底白字、画到页外、提示词注入，命中行从正文剔除 | 规则 |
| `resume_validate_claim` | 校验模型拆出的陈述，`category` 必须命中枚举，缺 `elements` 时按类别补缺省要素 | 规则 |
| `resume_plan_queries` | 按类别生成 2–5 条查询（含域名限定）；学历按设计不检索 | 规则 |
| `resume_classify_tier` | 给一条来源定 A/B/C/D，拿不准时先问它 | 规则 |
| `resume_judge_claim` | **核心**：身份判定 + 来源分级 + 转载去重 + 状态判定 | 规则 |
| `resume_summary` | 覆盖率统计，附产品边界声明 | 规则 |

三处刻意的架构决定：

- **来源等级与身份分不接受模型输入。** `resume_judge_claim` 的证据 schema 用
  `additionalProperties: false`，模型连填 `source_tier` / `identity_score` 的机会都没有——
  传了会在参数校验那一层被拒。纵深上，桥内部还会再重算一遍。
  这样落进会话日志的参数就等于模型真正提交的东西，不会"看起来是模型定的、其实是规则改的"。
- **模型自造的要素会被丢弃。** `supports` 只认该陈述 `elements` 里的原文，其余进 `dropped` 并如实报出。
- **不写死的都进 config。** `openBoxRoot` / `pythonBin` / `timeoutMs` 都是配置字段，
  改 `cordis.yml` 就能换，不需要动代码。

---

## 4. underbox 施工图：数据契约与落点

内置 Web Client **不消费** `presentCall` / `presentResult`（见
`docs/cookbook/adding-a-tool.zh.md` 的「Web Client 展示」一节）。
underbox 有两个注册入口，各管一层：

**① 会话流里的工具行**——每个 `resume_*` 一行，紧凑，点开看细节。
走 keyed slot `tool.call.toolview`，按 wire 工具名注册：

```ts
ctx.slots.inject('tool.call.toolview', () =>
  ctx.slots.register(
    { name: 'tool.call.toolview', key: 'resume_judge_claim' },
    JudgeClaimRow,
  ))
```

**② 完整报告**——右侧栏开一个自己的 tab `kind`。
不占用会话区宽度，可以 `push` 也能全屏；报告这种"要看很久"的内容适合放这儿。
用 `ctx.sidebarRightTabs` 注册 tab 类型，`ctx.sidebarRight` 负责导航过去。

数据走下面三条通路，按推荐顺序：

### 通路一（推荐）：`tool/result` 上的持久化 `meta`

每个工具的 `output.presentationMeta` 是**纯函数**（只依赖 args + 返回值），
产出的 JSON 会被 harness 持久化在 `tool/result` 的 `meta` 上，实时和**回放**都能拿到。

三种 `kind`：

```jsonc
// kind: "ingest"
{ "kind": "ingest", "source": "...", "riskCount": 6, "risks": [ /* kind/detail/excerpt/locator */ ] }

// kind: "verdict"  ← 主视图用这个
{
  "kind": "verdict",
  "claimId": "c05",
  "category": "学生工作",
  "rawText": "2024.03—2025.03　晴川大学校学生会 科技部 部长",
  "status": "ask",                 // ok | part | ask | none | who
  "statusLabel": "待澄清",
  "bestTier": "B",                 // A|B|C|D|null
  "proved": ["任职组织=晴川大学校学生会科技部", "起止时间=2024.03—2025.03"],
  "unproved": ["职务=部长"],
  "needsHuman": true,
  "searchExhausted": false,
  "nextStep": "发现具体不一致，请候选人说明后再决定如何记录，不要据此直接下结论。",
  "evidence": [{
    "url": "https://mp.weixin.qq.com/s/...",
    "title": "晴川大学校学生会第二十四届换届公告",
    "publisher": "晴川大学学生工作部",
    "sourceTier": "B",
    "snippet": "科技部：部长　周清让；副部长　林昱和、何知微。",  // 页面原文逐字摘录
    "publishedAt": "2024-03-18",
    "identityScore": 4,
    "identitySignals": ["学校一致", "学院一致"],
    "identityConflicts": [],
    "supports": ["任职组织=…", "起止时间=…"],
    "contradicts": ["职务=部长：公告中科技部部长为周清让，林昱和列于副部长"]
  }]
}

// kind: "summary"
{ "kind": "summary", "total": 11, "counts": { "ok":4, "part":2, "ask":1, "none":3, "who":1 },
  "coverage": { "found_public_source": "8/11（73%）", "tied_to_candidate": "…",
                "fully_proved": "…", "element_level": "…", "needs_human": "…" } }
```

### 通路二：自定义会话事件 `resume/verdict`

`resume_judge_claim` 每次判定后会往会话日志追一条事件，适合画核验看板 / 进度条，
不必翻遍每个 tool result：

```jsonc
{ "type": "resume/verdict", "data": { "claimId": "c05", "status": "ask", "bestTier": "B" } }
```

（事件是纯记录，不是唤醒——空闲的 agent 不会因此被叫醒。）

### 通路三：`tool/call` 事件 + 结果正文

`presentCall` 给的是 pending 卡片意图（`{card:'generic', kind, title, rawInput, locations}`），
`output.render` 给的是给模型看的自然语言。直接展示也可以，但结构化程度不如通路一。

### 五态配色（照抄，别自己发挥）

`status` 是封闭枚举，underbox 的标记色、统计带、图例都按这一个表来：

| status | 标签 | 色 | 为什么是这个色 |
| --- | --- | --- | --- |
| `ok` | 已证实 | 墨绿 `#0F6E56` | 唯一可以"看着就放心"的状态 |
| `part` | 部分证实 | 琥珀 `#854F0B` | 一半有据一半没据，属于正常，不是问题 |
| `ask` | 待澄清 | 橙 `#D85A30` | **不要用正红**——`ask` 只是"发现不一致，需说明"，可能是后续接任或口径不同 |
| `none` | 未找到公开记录 | 中性灰 `#5F5E5A` | 公开痕迹少不是污点 |
| `who` | 身份未确认 | 中性灰 `#5F5E5A` | 同名不是过错，转人工而已 |

`none` 和 `who` **必须同色**——两者都是"这条没结论"，把它俩画成不同深浅会暗示一个是问题。

### 两条产品约束（硬性，沿用 open_box 的设计决定）

1. **`none`（未找到公开记录）和 `who`（身份未确认）用中性灰，不要用红色。**
   公网痕迹少不是污点。
2. **不要出现总分、真实性百分比、评级、排名或录用建议。**
   `identityScore` 是"这条记录是不是本人"的身份关联度，只属于证据层，不是候选人的分数。
   `summary.disclaimer` 里写明了这条边界，underbox 也请照此呈现——那句声明要**常驻**，
   不要塞进"了解更多"里。

### 想做客户端插件的话

dsh 的扩展点是 **keyed slot `tool.call.toolview`**：客户端插件在其中注册自己关心的
wire 工具名（这里是 `resume_ingest` … `resume_summary`），然后从 `ToolCallBlock` 的
参数、内容、错误、metadata 以及 Session 路径事实派生组件 props。
两条硬约束（见 `docs/cookbook/adding-a-tool.zh.md`）：

- 客户端要**自己在本地校验**这些 wire 值，格式不对或版本旧了要回退到 generic 行，
  绝不能因为展示层的问题让回放崩掉；
- 不要把 Host 侧的工具实现 import 进浏览器 bundle，也不要另起一套 client presenter registry。

`result.meta`（通路一）就是为这件事准备的持久化通道——实时流和历史回放拿到的是同一份数据。

### 可选：把状态做成 Cordis projection

如果你想要一个可 `resume` / `fork` 自动重建的状态树，可以注册 session projection
（参考 `packages/todo/tool-todo` 的写法）。本插件没做，是因为它需要 `zod` 依赖，
而事件 + `result.meta` 两条通路已经够用。要做的话在 `apply()` 里加：

```ts
ctx.inject(['sessionProjections'], (projectionCtx) => {
  projectionCtx.sessionProjections.register({
    key: 'resumeVerification',
    schema: /* zod schema */,
    init: () => ({}),
    apply: (state, event) =>
      event.type === 'resume/verdict'
        ? { ...state, [event.data.claimId]: event.data }
        : state,
    view: s => s,
    stateVersion: 1,
  })
})
```

---

## 5. 验收

两套，都不需要 harness 就能跑。

### 5.1 规则层未漂移（11/11）

把 open_box 的 fixture 证据剥掉"由规则算出来的字段"，重新过一遍桥，
比对 `status` 与 `best_tier` 是否与 open_box 原结论逐条一致。

```sh
PYTHONPATH=/path/to/open_box python python/test_equivalence.py
```

> 这一步的存在意义：证明判定规则搬家之后没有变形。当前 **11/11 一致**。

### 5.2 插件与工具（67 项）

需要先让 `@deepseek-ai/dsh-tools` 能解析——也就是本目录下要有一个 `node_modules`
（本地开发时它是一个指向隔离环境的软链；你自己跑的话 `npm install` 一次即可）：

```sh
npm install                      # 装 peer 依赖（cordis / dsh-tools / schemastery）
node --experimental-strip-types test/offline-check.mjs
```

覆盖：6 个工具都注册上、参数校验会挡下非法输入、每个工具真能调到规则层、
返回值通过 `output.schema` 校验（不通过的话在 dsh 里会变成 isError）、
render 与 presentationMeta 的形状。

其中几条是刻意设的防回归：

- 模型自填 `source_tier` / `identity_score` → 被边界拒绝
- 桥内部仍会重算（纵深覆盖）
- 自造要素被收窄并如实报出
- **有 `identity_conflicts` 时，3 个身份信号（5 分）也照样判 `who`**
- 无证据 / 预算耗尽都判 `none`

---

## 6. 模型凭据配在哪

dsh 的凭据由 `@deepseek-ai/dsh-credentials-local` 管，**明文密钥只存在一个私有文件里**，
配置文件（`cordis.yml`）只写凭据的**名字**。

```sh
# 真实位置（默认 harness home）
~/.dsh/.credentials.yaml      # 权限 600，只有你的 OS 用户可读
```

```yaml
version: 1
refs:
  DEEPSEEK_API_KEY: sk-…
```

几个要点：

- **布局有版本号，且是全字符串的严格映射**，不是 dotenv。写成扁平结构或塞别的字段会被拒。
- **优先级**：`启动环境变量 > .credentials.yaml > 调用目录的 .env > $DSH_HOME/.env`。
  所以 `DEEPSEEK_API_KEY=… dsh web` 这类一次性覆盖永远说得算（CI、容器依赖这一点）。
- 也可以通过 Web 的 Models 页保存，效果相同，改完立即生效、跨重启保留。
- 当前可选模型（用 `/models` 端点核对过）：**`deepseek-flash`**、`deepseek-v4-pro`。

> ⚠️ `deepseek-flash` **默认开启思考**，`reasoning_content` 与正文共用 `max_tokens`。
> 预算给小了（例如 32）会看到 `content` 是空串、`finish_reason=length`，
> 很容易误判成"JSON 模式偶发空内容"。核验是结构化抽取 + `temperature=0`，
> 要的是确定性而不是推理过程——**建议关掉：**
> ```bash
> # 一次性
> LLM_THINKING=disabled dsh web --patch …
> ```
> 或在 profile 的 provider 配置里固定下来。

### 还没打通的：headless profile 跑不了工具

`dsh --profile headless "…"` 能连上模型（纯对话回复正常），但**一调工具就报**
`Cannot read properties of undefined (reading 'prepare')`。
对照实验：内置 `bash` 工具报**一模一样**的错，所以不是本插件的问题。

原因是 `headless` profile（87 个插件）没有接上 `dsh-agent-loop` 消费的
`ToolRuntimeScheduler`，而 `web` profile（152 个插件）有。
这是 dsh `0.1.5-rc` 的 profile 缺口。**目前端到端请走 `dsh web`。**

---

## 7. 目录

```
src/index.ts                   Cordis 插件：Config + 6 个工具 + session 事件
python/resume_rules.py         规则层桥：JSON stdio，6 个命令
python/test_equivalence.py     等价性验收（11/11）
test/offline-check.mjs         插件与工具验收（67 项）
install.sh                     装进 $DSH_HOME/profiles 并生成路径正确的 patch
cordis.yml                     挂载 patch 模板（内含"插件该放哪"的说明）
```

---

## 8. 已知边界与后续

- **本插件只做了规则层。** 检索与摘录交给 harness 的 web 工具和你配的模型；
  如果想固化检索策略，下一步是补一个 `resume_search` 工具把 `plan_queries` 的输出直接喂给搜索 API。
- **公众号仍只有两条合规通路**（通用搜索已收录的链接、候选人自提供链接），
  这是 open_box 原本就认定的产品上限，套进 harness 不会改变它。
- **`organizer_domain` 有断点**：`plan.py` 和 `collect.py` 都读 `claim.entities.organizer_domain`，
  但 open_box 的拆分 prompt 从没让模型产出过这个字段——fixture 能过是因为构建脚本硬编码了
  `ORGANIZER_HOSTS`。赛事类陈述若拿不到组委会域名，其官网页面会掉到 D 级，永远判不成 `ok`。
  本插件的 `resume_validate_claim` 已经在 `entities` 的说明里点明要填它，但要根治得改 open_box 那边。
- **已验证到"模型真的会调这个工具"这一步，但没能跑完一次完整的编排。**
  实测确认：插件在真实 dsh 里加载、`inject: ['tools']` 解析、6 个工具进工具表、配置正确读入；
  接上真实凭据后，模型也确实推理出"该用 `resume_ingest` 了"。但**工具一执行就报**
  `Cannot read properties of undefined (reading 'prepare')`——这是 `headless` profile 的
  缺口（见 §6），内置 `bash` 工具同样报错，与本插件无关。
  **所以端到端要起 `dsh web`**，那条路径上工具调度是完整的。
- **`ctx.logger` 在默认装配下不出现在终端上**（实测确认）。插件的启动日志因此用的是
  `console.log`，与官方教程的 `hello-plugin` 一致。要正经的可观测性，走会话日志。

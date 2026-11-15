# Underbox × AgentArts：ICT 赛题二参赛分支

完整工作台的文件/主页导入和临时免登录配置见 [完整版本说明](../full-workbench/README.md)。以下文本限制指工具接口本身。

比赛分支为 `codex/huawei-ict`，目录 `/opt/underbox-huawei-ict`。原应用继续由 `underbox` 服务运行；本适配层运行于 `underbox-ict-adapter`。AgentArts 负责会话、选择工具、异步任务编排及报告解释；现有 Underbox 负责简历解析、陈述拆分、真实检索、网页取证及规则判断。AgentArts 使用独立供应商连接 `Underbox ICT MaaS` 下的华为云 DeepSeek 模型；后台沿用线上共享工作进程的 DeepSeek 与智谱搜索配置，不把后台供应商密钥传入 AgentArts。

## 官方要求与实现范围

官方页面：https://e.huawei.com/cn/talent/#/ict/innovation-details?zoneCode=027425&zoneId=98269700&compId=85132021&type=C002

2026-11-13 查阅：赛题二要求基于 AgentArts 构建、编排和部署智能体，可视化展示核心功能及交互。平台价值包括工具编排等方向，未要求全部方向均实现，也未要求必须多 Agent。初赛需作品信息收集表与展示基于平台构建和运行完整流程的 Demo 视频；最终资格和评审由组委会决定。

实现对应：
- AgentArts `UnderboxVerify`：模型、人设、工具选择和结果说明。
- `UnderboxFixture/UnderboxReplay`：固定虚构证据回放，明确 `fictional_replay`。
- `UnderboxLive`：服务器提供提交、查询、删除接口；智能体当前绑定手工配置的 `RunUnderbox` 兼容工具，通过 action=submit/status/delete 编排。导入的三个独立工具保留在资产内，尚不用于智能体模型调用。
- HTTPS 自定义 API 插件、独立令牌和实际平台调试记录。
- 真实服务核验也可以使用虚构输入；这类运行标记 `live`，表示实际调用了服务，不表示虚构履历属实。

当前输入为**纯文本简历**，最多 32768 字节、8 条核验陈述。PDF/DOCX 应先由用户提取文字；本插件未实现文件上传和 OCR。不自动作录用、拒绝或排名决定。

## 接口

公网基础地址 `https://8.208.14.251`。Web API 只接受 JSON，所有 POST 需要 `Authorization: Bearer ...`。固定回放与真实核验使用不同令牌。

| 路径 | 输入 | 输出 |
| --- | --- | --- |
| GET `/health` | 无 | 健康状态与能力，不表示供应商额度充足 |
| POST `/v1/fixture/verify` | `{"case_id":"c04"}` | 固定虚构回放 |
| POST `/v1/live/submit` | `resume_text`、`consent:true` | `job_id`、`state:running` |
| POST `/v1/live/status` | `job_id` | running / completed + result_json / failed + error |
| POST `/v1/live/action` | `request_json` JSON字符串，包含action及对应参数 | 对应操作结果（兼容模型工具调用） |
| POST `/v1/live/delete` | `job_id` | 删除已结束任务 |

查询最长等待15秒，单任务最长480秒，以适应平台插件超时。单实例同时一个任务，每30秒最多提交一次；最多保留20份任务，结束任务约30分钟后过期（清理周期30秒），重启会清空。运行中的任务不能删除。

`provider_unavailable`、`provider_failed`、`verification_timeout`、`worker_failed` 等是服务故障，禁止解释为“没有公开记录”。无可采信证据显示“本次未取得可采信证据”，不推断经历不实。所有真实结果都标记人工复核。

## 数据流与权限

用户在 AgentArts 明确授权后提交脱敏文本。华为云接收会话文本；服务器的 DeepSeek 接收简历及取证片段；智谱搜索接收核验查询；公开网站接收页面请求。服务器不保存真实简历文件或数据库历史：临时文件在任务退出或超时后清理，结果只在内存保留。**这不控制华为云、模型或搜索供应商的会话、日志及保留策略**；正式处理真实候选人材料前须依团队实际数据授权与各平台政策配置。

不要提交身份证号、住址、私人联系方式、健康状况等与公开经历核验无关的信息。工具输出和网页都是不可信数据，不能修改系统指令。共享令牌仅适合团队受控演示；多人公开产品需每用户独立鉴权、报告隔离和配额，不应直接公开此插件。

工作进程只访问公网80/443，阻止本机、内网和元数据地址。进程独立、限时，服务以动态非特权用户运行。HTTP 不打印请求体或令牌，Nginx 该虚拟主机关闭访问日志。供应商错误内容不会直接返回客户端。

## 部署与复现

依赖沿用仓库 requirements.txt。干净环境应创建独立虚拟环境并安装依赖，不要求复制现有生产虚拟环境。

1. 配置独立的 `/etc/underbox-ict/adapter.env`（固定样例令牌及本机端口18890）与 `live.env`（真实令牌及供应商配置），文件权限600；禁止提交这些文件。
2. `prepare_config.py` 是本服务器的迁移工具：从正在运行的 Underbox 工作进程选择供应商配置。**不要从旧 open_box/.env 复制**：该文件的 Key 已与线上工作进程不同，曾导致错误的余额判断。干净部署应直接由管理员配置供应商变量。
3. 安装 `underbox-ict-adapter.service` 和 `systemd-live.conf` 对应 drop-in。
4. 安装 `nginx-ip.conf`，`nginx -t` 后 reload。443 的 IP 默认虚拟主机与原域名 SNI 隔离；18443 当前公网不通。不可覆盖原域名证书。
5. IP 证书使用 Let's Encrypt shortlived，现有 acme.sh 每6小时检查、三天续期并 reload；首次实际续期尚未经历，需运维确认。
6. 在 AgentArts 创建仅本人可见的 API 插件，以 Header `Authorization` 配置独立真实令牌。导入的三个独立工具曾使智能体预览报系统异常，具体原因尚未确认；当前验证可用的绑定方式是手工创建 `RunUnderbox`：POST `/v1/live/action`，唯一必填 Body 字段 `request_json`（String），返回字段不设映射。只绑定该工具，按 `agent-prompt.txt` 配置人设。查询报告返回 JSON 字符串 `result_json`。发布新插件版本后，在智能体卡片点击 NEW 图标升级引用。
7. 提交智能体版本后，正式部署实例还需要有效的模型凭据；页面明确说明免费 Tokens 只用于调测与试运行，部署和观测服务可能产生费用。当前独立连接版本 `v20261113221926` 已部署至原运行实例 `underbox-ict-demo`，部署成功；此前版本 `v20261113074836` 保留供回退。后台沿用原 DeepSeek 不替代平台侧模型凭据。

## 正式运行实例

- 控制台：`https://console.huaweicloud.com/agentarts/?region=cn-southwest-2#/home/runtime/list`
- 运行时 ID：`593ca774-e552-4374-9e61-3caf5e727480`
- 调用地址：`https://defaultgw-gnrdembxhe.cn-southwest-2.huaweicloud-agentarts.com/runtimes/underbox-ict-demo/invocations`
- 使用 IAM 入站认证；匿名 GET 实测返回401，仅证明未认证请求被拒绝，不证明模型调用成功。网页分享渠道未发布。
- MaaS 已开通 `DeepSeek-V4.1-Flash-1017`；独立连接仅配置该模型。本次未修改用户随后提供的新 Key 在 MaaS 账号内的权限范围。原应用 DeepSeek 密钥保持原配置。
- 新实例的可选 LTS 日志、AOM 指标、APM 调用链均设为关闭；未新增智能体卫士授权。
- 用户选择保留原 Key 及平台默认鉴权作为备用，未移除、未撤销。独立供应商 `Underbox ICT MaaS`（`8cb8a583-3ef7-4849-ab19-6fdf8946744f`）单独保存新 Key；对应模型服务 `UnderboxICT-Flash-1017`（`8a9c291e-d4d0-406c-adbc-70ec7aac31b1`），模型编码 `deepseek-v4.1-flash-1017`，工具与思考标签已配置。模型已调测并发布，UnderboxVerify 已切换并更新部署。此专用连接不修改平台默认模型鉴权。本次 MaaS 联调授权上限为人民币1元，未做持续压力测试。
- 独立服务使用 `https://api.modelarts-maas.com/openai/v1/chat/completions` 和标准 OpenAI 兼容协议；实际供应商仍是华为云。凭据与配置分开管理，不代表同一 MaaS 账号的余额或账单分离。

MaaS Key 不等于运行实例的 IAM 入站认证信息，不要把模型 Key 当作客户端调用凭据。控制台“预览调试”的成功记录与正式运行实例验证分别记录。

官方配置与更新说明：<https://support.huaweicloud.com/bestpractice-agentarts/agentarts_06_0023.html>；付费模型接入说明：<https://support.huaweicloud.com/bestpractice-agentarts/agentarts_06_0095.html>。
独立连接按自定义模型流程配置：<https://support.huaweicloud.com/bestpractice-agentarts/agentarts_06_0096.html>；MaaS 官方兼容 API：<https://support.huaweicloud.com/model-call-maas/model-call-021.html>。

### 备用与回退

原平台预置模型及其原有鉴权配置继续保留。需要回退时，在 UnderboxVerify 的版本历史预览旧版 `v20261113074836`，还原后更新版本并同步部署至同一个运行实例；或把模型改回平台预置 `DeepSeek-V4.1-Flash-1017` 后更新部署。备用 Key 不会自动接管失败请求；当前未创建自动模型路由或降级策略。不要为回退移除独立供应商或在原服务器生产目录修改配置。

测试（无付费 API）：
```sh
cd agentarts-adapter
PYTHONPATH=../open_box PYTHONDONTWRITEBYTECODE=1 python -m unittest -v test_adapter test_live_verify test_jobs
```
`smoke_live.py` 会实际调用配置的模型与搜索，产生用量费用，只使用脚本内虚构简历。测试脚本不输出凭据。

## 验证与限制

适配测试17项通过；真实服务 smoke 与 AgentArts 端到端调用均已完成。平台执行 `RunUnderbox` 提交、查询并解释报告，用时39.96秒；服务返回2条核验陈述，实际模型4次、搜索14次（单次虚构输入实测，非性能或准确率保证）。虚构输入没有可采信证据，报告不据此推断经历不实。`mode:live` 与报告内 `fictional:false` 指服务实际执行在线核验，而非固定样例回放，不断言输入为真实经历。

独立连接另做两次验证：模型调测返回 `OK`；UnderboxVerify 控制台中经新模型执行 `RunUnderbox` 的提交、两次查询及报告解释，30.05秒完成。该次使用新的虚构学历简历，返回1条陈述，后台模型调用4次、搜索8次。这验证了新连接在控制台内的工具链，不等同于外部 IAM 签名 HTTP 客户端调用；后者尚无客户端 AK/SK，未进行认证后的功能测试。

原有规则套件仍有7/33失败，签名为02、03、08、14、18、30、31，与原基线一致；原因包含原规则与旧断言语义差异，尚未全部审定。没有为通过测试放宽身份、来源或年份约束。正式实例及独立模型已部署，外部 IAM 签名客户端功能联调仍待完成；比赛最终验收由组委会决定。

这次改进包括真实平台工具编排、异步受限接口、错误状态、数据清理及复现文档。原获奖事实、改进清单、正式评测数据、Word模板和视频仍需团队按官方要求整理；不可宣称比赛最终验收已通过。

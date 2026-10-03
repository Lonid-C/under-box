# 公开资料检索试跑（2026-10-02）

这次只使用公开人物和公开成果，没有使用真实求职者简历。目标是验证「学校/赛事/论文 → 对应官方来源 → 姓名/成果」这条路径是否可行，并暴露程序中的漏检。

| 类型 | 查询目标 | 找到的官方来源 | 本地读页结果 |
| --- | --- | --- | --- |
| 学历线索 | UCLA / Hayden Schaeffer | [UCLA 数学系教师页](https://www.math.ucla.edu/people/ladder/hayden)，写有姓名及 2013 年 UCLA 数学博士经历 | 可读取正文，包含姓名和学历线索 |
| 比赛获奖 | ICPC World Finals 2024 / Wang Weicheng | [ICPC 官方新闻](https://news.icpc.global/wf2024/)，列出冠军队伍和队员姓名 | 可读取正文，包含姓名、学校和队伍 |
| 论文发表 | BERT / Jacob Devlin / NAACL 2019 | Crossref 登记记录的原文链接指向 [ACL Anthology 论文页](https://aclanthology.org/N19-1423/) | 可读取题名、作者和刊物 |
| 论文发表 | Attention Is All You Need / Ashish Vaswani / NeurIPS 2017 | [NeurIPS 正式论文页](https://proceedings.neurips.cc/paper_files/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html) | 可读取；修复后正文会保留页首作者 |

据此修正了四个实际问题：明确写成英文校名或简称的学历陈述不再漏掉学校官网检索；ICPC、NeurIPS、NAACL/ACL/EMNLP 等已核对的赛事或论文存档域可直接限定；Crossref 优先使用登记的 `resource.primary.URL` 找原文页，并按 DOI 区分同名论文、用年份过滤明显不符的记录；网页正文提取保留页首的题名和作者。

当前部署仍未通过**真实搜索 API**的端到端验收：`make search-check` 中智谱返回「余额不足或无可用资源包」，本机也没有配置 `BRAVE_SEARCH_API_KEY`。上表的发现阶段使用了独立的公开网页搜索，后续读页使用项目自身的 `PageFetcher`。对禁止抓取或无法连接的官网，项目不会强行读取；搜索结果存在也不应被当作已核实的简历事实。真实个人的同名消歧和结论仍需逐条查看原文。

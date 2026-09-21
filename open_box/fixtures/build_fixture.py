#!/usr/bin/env python3
"""生成 fixtures/report_lin.json（第 9 节）。

要点：状态、来源等级、已证明/尚未证明**不是手写的**，而是把虚构证据
喂给 app.judge 的同一套规则算出来的。fixture 与规则因此不会各说各话，
第 11 节验收第 2 项才有意义。

候选人、学校、组织、赛事、期刊、仓库、链接全部虚构，
域名使用 RFC 6761/2606 保留的 .example，不指向任何真实站点。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import judge                                     # noqa: E402
from app.parse import parse_document                      # noqa: E402
from app.schema import Claim, Evidence, Report            # noqa: E402

ACCESSED = "2026-09-18T09:00:00+08:00"
ORGANIZER_HOSTS = {"icpc-qingchuan.example", "qcacm.example"}


def ev(url, title, publisher, snippet, *, signals=(), conflicts=(), supports=(),
       contradicts=(), published_at=None, origin_url=None, tier=None,
       wechat=None, media=False, repost=False) -> Evidence:
    """建一条证据。source_tier 默认由 judge.classify_tier 推出来，不手填。"""
    t = tier or judge.classify_tier(
        url, title, publisher, snippet,
        organizer_hosts=ORGANIZER_HOSTS,
        wechat_verified_subject=wechat,
        authoritative_media=media,
        is_repost=repost,
    )
    e = Evidence(
        url=url, title=title, publisher=publisher, source_tier=t, snippet=snippet,
        published_at=published_at, accessed_at=ACCESSED,
        identity_signals=list(signals), identity_conflicts=list(conflicts),
        supports=list(supports), contradicts=list(contradicts), origin_url=origin_url,
    )
    return judge.score_evidence(e)


def claim(cid, raw, locator, cat, label, start, end, elements, entities) -> Claim:
    return Claim(id=cid, raw_text=raw, raw_locator=locator, category=cat,
                 date_label=label, date_start=start, date_end=end,
                 elements=elements, entities=entities)


SCHOOL = "晴川大学"
DEPT = "计算机学院"
MAJOR = "计算机科学与技术"


def build() -> Report:
    items: list[tuple[Claim, list[Evidence], dict]] = []

    # 1 ── 学历：按第 6.3 节，不检索，直接 none + 授权补证
    c = claim("c01", "晴川大学 计算机学院 计算机科学与技术 本科在读　2022.09 — 2026.06",
              "第1页·教育经历·第1行", "学历", "2022.09", "2022-09-01", "2026-06-30",
              ["学校=晴川大学", "学院=计算机学院", "专业=计算机科学与技术", "学籍状态=本科在读"],
              {"org": SCHOOL, "dept": DEPT, "major": MAJOR, "level": "本科"})
    items.append((c, [], {
        "next_step": "学历以学信网在线验证报告核验，需候选人授权并提供验证码；本 demo 不做自动化查询。",
        "question": "方便提供学信网在线验证报告的验证码用于核验学历吗？",
    }))

    # 2 ── 校级三好学生：A 级公示 + 一篇转载（验证去重不会重复计数）
    c = claim("c02", "2023.05　晴川大学 校级三好学生", "第1页·荣誉与奖励·第1行",
              "校内荣誉", "2023.05", "2023-05-01", "2023-05-31",
              ["奖项=校级三好学生", "授予单位=晴川大学", "获奖学年=2022—2023学年"],
              {"org": SCHOOL, "dept": DEPT, "level": "校级"})
    official = "https://xsc.qingchuan.edu.example/tzgg/2023/0512/1837.html"
    items.append((c, [
        ev(official,
           "关于公布2022—2023学年校级三好学生名单的公示",
           "晴川大学学生工作部",
           "计算机学院：林昱和（2022级 计算机科学与技术）、何知微、周清让……公示期为2023年5月12日至5月18日。",
           signals=["学校一致", "学院一致", "年级一致"],
           supports=["奖项=校级三好学生", "授予单位=晴川大学", "获奖学年=2022—2023学年"],
           published_at="2023-05-12"),
        ev("https://campusnews.example/post/9921",
           "【转载】晴川大学公布2022—2023学年校级三好学生名单",
           "晴川校园资讯（个人站）",
           "计算机学院：林昱和（2022级 计算机科学与技术）、何知微、周清让……",
           signals=["学校一致"],
           supports=["奖项=校级三好学生"],
           published_at="2023-05-14", origin_url=official, repost=True),
    ], {
        "next_step": "学校学生工作部公示可直接采信，无需候选人补充材料。",
        "question": None,
    }))

    # 3 ── 院级学生工作：公网无记录
    c = claim("c03", "2023.06 — 2024.03　晴川大学计算机学院学生会 技术部 干事",
              "第2页·学生工作·第1行", "学生工作", "2023.06", "2023-06-01", "2024-03-31",
              ["任职组织=计算机学院学生会技术部", "职务=干事", "起止时间=2023.06—2024.03"],
              {"org": f"{SCHOOL}{DEPT}学生会", "dept": "技术部", "role": "干事", "level": "院级"})
    items.append((c, [], {
        "next_step": "院级学生组织的任免多数不在公网发布，检索无结果属正常情况；可请候选人提供任命通知或组织盖章证明。",
        "question": "这段院学生会的任职，能否提供任命通知或盖章证明？",
    }))

    # 4 ── 竞赛：主办方官网获奖名单，队友姓名对得上
    c = claim("c04", "2023.11　ICPC 亚洲区域赛 晴川站 铜奖（队伍：晴川大学 Trailing Zeros）",
              "第2页·竞赛经历·第1行", "竞赛", "2023.11", "2023-11-01", "2023-11-30",
              ["赛事=ICPC 亚洲区域赛 晴川站", "奖项=铜奖",
               "参赛身份=队伍 Trailing Zeros 成员", "时间=2023.11"],
              {"org": SCHOOL, "team": "Trailing Zeros", "level": "区域赛"})
    items.append((c, [
        ev("https://icpc-qingchuan.example/2023/awards.html",
           "2023 ICPC 亚洲区域赛晴川站 获奖名单公示",
           "ICPC 亚洲区域赛晴川站组委会",
           "铜奖　晴川大学　Trailing Zeros（陈屿、林昱和、周清让）　指导教师：方序",
           signals=["学校一致", "队友或合作者一致", "年级一致"],
           supports=["赛事=ICPC 亚洲区域赛 晴川站", "奖项=铜奖",
                     "参赛身份=队伍 Trailing Zeros 成员", "时间=2023.11"],
           published_at="2023-11-20"),
    ], {
        "next_step": "主办方官网名单为队伍奖项，个人贡献不在名单范围内，如需了解可在面试中询问分工。",
        "question": None,
    }))

    # 5 ── 重点条目：简历写"部长"，官方换届公告写"副部长"
    c = claim("c05", "2024.03 — 2025.03　晴川大学校学生会 科技部 部长",
              "第2页·学生工作·第2行", "学生工作", "2024.03", "2024-03-01", "2025-03-31",
              ["任职组织=晴川大学校学生会科技部", "职务=部长", "起止时间=2024.03—2025.03"],
              {"org": f"{SCHOOL}校学生会", "dept": "科技部", "role": "部长", "level": "校级"})
    items.append((c, [
        ev("https://mp.weixin.example/s/Qc24-xsh-huanjie",
           "晴川大学校学生会第二十四届换届公告",
           "晴川大学学生工作部（微信公众号，认证主体：晴川大学）",
           "科技部：部长　周清让；副部长　林昱和、何知微。任期自2024年3月起，至2025年3月换届。",
           signals=["学校一致", "学院一致"],
           supports=["任职组织=晴川大学校学生会科技部", "起止时间=2024.03—2025.03"],
           contradicts=["职务=部长：公告中科技部部长为周清让，林昱和列于副部长"],
           published_at="2024-03-18",
           wechat="晴川大学"),
    ], {
        "next_step": "任职组织与起止时间与公告一致，职务表述存在差异。请候选人说明后再决定如何记录，不要据此直接下结论。",
        "question": "换届公告里科技部部长写的是另一位同学，你的职务是后来接任的吗？",
    }))

    # 6 ── 国家奖学金
    c = claim("c06", "2024.05　国家奖学金", "第1页·荣誉与奖励·第2行",
              "奖学金", "2024.05", "2024-05-01", "2024-05-31",
              ["奖项=国家奖学金", "评审学年=2023—2024学年", "授予单位=晴川大学"],
              {"org": SCHOOL, "dept": DEPT, "major": MAJOR, "level": "国家级"})
    items.append((c, [
        ev("https://xsc.qingchuan.edu.example/tzgg/2024/0520/2101.html",
           "关于公布2023—2024学年国家奖学金获奖学生名单的通知",
           "晴川大学学生工作部",
           "计算机学院　林昱和　计算机科学与技术　2022级……以上学生获2023—2024学年国家奖学金。",
           signals=["学校一致", "学院一致", "专业一致"],
           supports=["奖项=国家奖学金", "评审学年=2023—2024学年", "授予单位=晴川大学"],
           published_at="2024-05-20"),
    ], {
        "next_step": "学校通知可直接采信，无需候选人补充材料。",
        "question": None,
    }))

    # 7 ── 重点条目：简历写"独立开发"，仓库另有 2 名贡献者
    c = claim("c07", "2024.09 起　qingchuan-scheduler：面向校园场地预约的调度库，独立开发",
              "第3页·项目与论文·第1行", "开源项目", "2024.09", "2024-09-01", None,
              ["项目=qingchuan-scheduler", "角色=独立开发", "起始时间=2024.09"],
              {"repo": "linyuhe/qingchuan-scheduler", "role": "独立开发"})
    items.append((c, [
        ev("https://github.example/linyuhe/qingchuan-scheduler",
           "linyuhe/qingchuan-scheduler",
           "GitHub 仓库元数据",
           "Created 2024-09-08 · Contributors 3 · linyuhe 312 commits · hejw 56 commits · zhouqr 32 commits",
           signals=["账号由候选人提供", "候选人自提供该链接"],
           supports=["项目=qingchuan-scheduler", "起始时间=2024.09"],
           published_at=None),
    ], {
        "next_step": "仓库存在、创建时间与简历一致。“独立开发”属自述表述，公开数据只能显示提交分布，不足以支持或否定，建议面试中了解分工。",
        "question": "仓库显示另有两位贡献者，方便说明一下各自负责的部分吗？",
        "unproved_notes": {
            "角色=独立开发": "仓库另有 2 名贡献者，本人提交占 78%；提交分布不足以支持或否定“独立开发”的表述",
        },
    }))

    # 8 ── 重点条目：简历写"第一作者"，Crossref 署名第二位
    c = claim("c08", "2025.01　《基于时序约束的校园场地调度算法》，第一作者，《晴川计算学报》",
              "第3页·项目与论文·第2行", "论文", "2025.01", "2025-01-01", "2025-01-31",
              ["论文标题=《基于时序约束的校园场地调度算法》", "作者位次=第一作者",
               "发表时间=2025.01", "发表载体=《晴川计算学报》"],
              {"org": SCHOOL, "journal": "晴川计算学报", "role": "第一作者"})
    items.append((c, [
        ev("https://api.crossref.example/works/10.9999/qcjc.2025.0117",
           "基于时序约束的校园场地调度算法 · Crossref 元数据",
           "Crossref",
           '"author":[{"family":"Zhou","given":"Qingrang","sequence":"first"},'
           '{"family":"Lin","given":"Yuhe","sequence":"additional"}],'
           '"container-title":["晴川计算学报"],"issued":{"date-parts":[[2025,1]]}',
           signals=["队友或合作者一致", "学校一致"],
           supports=["论文标题=《基于时序约束的校园场地调度算法》", "发表时间=2025.01",
                     "发表载体=《晴川计算学报》"],
           published_at="2025-01-17"),
    ], {
        "next_step": "论文本身可核实。作者位次与简历写法不一致，但 Crossref 不记录共同一作标注，不能据此认定简历有误，请候选人说明。",
        "question": "论文的作者顺序与简历写法不同，是否存在共同一作或署名调整？",
        "unproved_notes": {
            "作者位次=第一作者": "Crossref 作者列表中本人列于第二位（sequence=additional）；该接口不记录共同一作标注，不足以据此判定简历表述有误",
        },
    }))

    # 9 ── 重点条目：省赛名单有同名者，但学校不同
    c = claim("c09", "2025.04　晴川省大学生程序设计竞赛 二等奖", "第2页·竞赛经历·第2行",
              "竞赛", "2025.04", "2025-04-01", "2025-04-30",
              ["赛事=晴川省大学生程序设计竞赛", "奖项=二等奖",
               "参赛单位=晴川大学", "时间=2025.04"],
              {"org": SCHOOL, "dept": DEPT, "major": MAJOR, "level": "省级"})
    items.append((c, [
        ev("https://mp.weixin.example/s/qcacm-2025-list",
           "2025年晴川省大学生程序设计竞赛获奖名单",
           "晴川省计算机学会（微信公众号，认证主体：晴川省计算机学会）",
           "二等奖　林昱和　晴川师范学院　软件工程　指导教师：许放",
           signals=[],
           conflicts=["学校不同：晴川师范学院", "专业不同：软件工程"],
           supports=[],
           published_at="2025-04-28",
           wechat="晴川省计算机学会"),
    ], {
        "next_step": "名单中同名者所属学校与专业均与候选人不符，不能计入本人。请候选人提供获奖证书编号或参赛队信息后再核。",
        "question": "省赛名单里的同名同学来自另一所学校，能提供你的获奖证书编号吗？",
    }))

    # 10 ── 实习：公开检索命中率低
    c = claim("c10", "2025.07 — 2025.09　澜山科技 算法实习生，参与推荐召回模块的离线评估",
              "第3页·实习经历·第1行", "实习", "2025.07", "2025-07-01", "2025-09-30",
              ["任职公司=澜山科技", "职务=算法实习生",
               "起止时间=2025.07—2025.09", "工作内容=参与推荐召回模块的离线评估"],
              {"org": "澜山科技", "role": "算法实习生"})
    items.append((c, [], {
        "next_step": "企业实习通常没有公开记录，检索无结果不指向任何问题。可在面试中请候选人说明工作内容，或由候选人自行提供实习证明。",
        "question": "方便提供这段实习的证明材料，或在面试中说明具体负责的部分吗？",
    }))

    # 11 ── 优秀毕业生
    c = claim("c11", "2026.03　晴川大学 校级优秀毕业生", "第1页·荣誉与奖励·第3行",
              "校内荣誉", "2026.03", "2026-03-01", "2026-03-31",
              ["奖项=校级优秀毕业生", "授予单位=晴川大学", "获评年份=2026"],
              {"org": SCHOOL, "dept": DEPT, "major": MAJOR, "level": "校级"})
    items.append((c, [
        ev("https://xsc.qingchuan.edu.example/tzgg/2026/0310/2988.html",
           "关于公布2026届校级优秀毕业生名单的公示",
           "晴川大学学生工作部",
           "计算机学院　林昱和　计算机科学与技术……公示期为2026年3月10日至3月16日。",
           signals=["学校一致", "学院一致", "专业一致"],
           supports=["奖项=校级优秀毕业生", "授予单位=晴川大学", "获评年份=2026"],
           published_at="2026-03-10"),
    ], {
        "next_step": "学校公示可直接采信，无需候选人补充材料。",
        "question": None,
    }))

    # ── 全部走同一套规则 ────────────────────────────────────────────────
    verified = []
    for c, evidences, extra in items:
        vc = judge.assess(c, evidences)
        vc.next_step = extra["next_step"]
        vc.question = extra.get("question")
        notes = extra.get("unproved_notes", {})
        vc.unproved = [f"{u}｜{notes[u]}" if u in notes else u for u in vc.unproved]
        verified.append(vc)

    parsed = parse_document(ROOT / "fixtures" / "resume_lin.md")

    return Report(
        id="lin",
        candidate_name="林昱和",
        position="算法工程师（校招）",
        report_no="QC-2026-0918-001",
        generated_at=ACCESSED,
        mode="mock",
        source_file="fixtures/resume_lin.md",
        fictional=True,
        fictional_notice="候选人与来源均为虚构",
        input_risks=parsed.risks,
        claims=verified,
    )


if __name__ == "__main__":
    report = build()
    out = ROOT / "fixtures" / "report_lin.json"
    out.write_text(json.dumps(report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")
    for vc in report.claims:
        print(f"  {vc.claim.id}  {vc.status:5s} tier={vc.best_tier or '—':2s} "
              f"human={'Y' if vc.needs_human else 'n'}  {vc.claim.date_label}  {vc.claim.category}")
    print("  counts:", report.counts())

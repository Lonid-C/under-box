"""Category specialists with bounded tools and report-local, traceable collaboration.

Specialists guide retrieval/extraction; judge remains the only proof decision.
Shared facts are navigation hints, never evidence for another claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import re


@dataclass(frozen=True)
class VerificationAgent:
    id: str
    name: str
    categories: tuple[str, ...]
    sources: tuple[str, ...]
    instructions: str

    @property
    def source_ids(self):
        shared = ("public_web", "school_catalog")
        return {
            "education": (*shared, "school_records"),
            "employment": shared,
            "paper": (*shared, "paper_metadata", "publisher_records"),
            "book": (*shared, "book_catalogs", "publisher_records"),
            "patent": (*shared, "patent_records"),
            "conference": (*shared, "conference_records"),
            "award": (*shared, "school_records"),
            "competition": (*shared, "school_records", "competition_catalog", "competition_records"),
            "internship": ("qcc_contacts",),
            "project": (*shared, "github_records"),
            "teaching": shared,
            "art": shared,
            "student_work": (*shared, "school_records"),
        }[self.id]

    @property
    def query_kinds(self):
        return {"paper": ("web", "crossref"), "patent": ("web", "patent"),
                "project": ("web", "github"), "internship": ("company_contact",)}.get(self.id, ("web",))

    @property
    def dependencies(self):
        # Authored works can establish a third-party identity route for faculty;
        # only selected tasks exist in the dependency graph.
        return ("paper", "book") if self.id in {"employment", "teaching", "art"} else ()

    def describe(self):
        from .data_sources import SOURCES
        return {"id": self.id, "name": self.name, "dataSources": list(self.sources),
                "tools": [SOURCES[key].describe() for key in self.source_ids],
                "queryKinds": list(self.query_kinds), "dependsOn": list(self.dependencies),
                "objective": self.instructions}

    def refine_plan(self, claim, candidate_name, plan):
        """Keep existing budgets/routes, but avoid unscoped name searches for jobs.

        Employer domains are still filtered by SourceGuard; a job target in the
        query is an identity anchor, not permission to use that employer's site.
        """
        from .strategy import github_trigger
        selected = []
        for q in plan.queries:
            if q.kind not in self.query_kinds or q.kind == "github" and not github_trigger(claim):
                plan.dropped.append(f"{self.name}不调用无关数据源：{q.kind}")
            else:
                selected.append(q)
        plan.queries = selected
        if self.id == "employment":
            org = _value(claim, ("company", "org"), ("单位", "公司", "机构", "任职单位", "学校"))
            role = _value(claim, ("role",), ("职务", "岗位", "职位", "职称"))
            # Only add verbatim job/company tokens; model-invented fields cannot
            # narrow the search to an unrelated company or occupation.
            if org and _compact(org) in _compact(claim.raw_text):
                from dataclasses import replace
                selected = []
                for q in plan.queries:
                    if q.kind != "web" or not candidate_name or candidate_name not in q.text:
                        selected.append(q)
                        continue
                    text = q.text
                    if _compact(org) not in _compact(text):
                        text += ' "' + org.replace('"', '') + '"'
                    # Keep historical-year probes broad enough to find records;
                    # strengthen only a pure company/person query with the role.
                    if (role and _compact(role) in _compact(claim.raw_text)
                            and not re.search(r"(?:19|20)\d{2}|任命|任免|公告|经历|履历", text)
                            and _compact(role) not in _compact(text)):
                        text += ' "' + role.replace('"', '') + '"'
                    selected.append(replace(q, text=text, purpose=q.purpose + "；核对本人单位和岗位，向独立第三方取证"))
                plan.queries = list({(q.text, q.site, q.kind): q for q in selected}.values())
        # Concrete authored-work anchors support an external search route. They
        # never add a school/role/award/degree to claim.elements or proved.
        if self.id in {"employment", "teaching", "art"} and candidate_name and plan.budget.get("searches", 0):
            from .strategy import Query
            facts = claim.entities.get("verified_identity_anchors", [])
            for fact in facts:
                field, _, value = str(fact.get("element", "")).partition("=")
                if (fact.get("category") not in {"论文", "著作"} or not fact.get("urls")
                        or field not in {"论文标题", "论文题目", "书名", "著作名称"} or not 3 <= len(value) <= 200):
                    continue
                purpose = "从已核实作品定位本人第三方履历；作品不能证明本条岗位、教学或演出"
                suffix = {"employment": "作者简介 任职", "teaching": "授课 课程", "art": "演出 节目"}[self.id]
                query = Query(text='"' + candidate_name.replace('"', '') + '" "' + value.replace('"', '') + '" ' + suffix,
                              source="collaboration:" + fact["claimId"], purpose=purpose, round=1, weight=146)
                if not any(q.text == query.text for q in plan.queries):
                    plan.queries.insert(0, query)
                    plan.handoffs.append({"fromClaim": fact["claimId"], "toClaim": claim.id,
                                          "element": fact["element"], "urls": list(fact["urls"]),
                                          "use": "navigation_only", "query": query.text})
                break  # At most one new query, within the unchanged task budget.
        plan.notes.append(self.name + "：" + self.instructions)
        return plan

    def collect(self, run, claim, name, searcher, llm, *, collector, manual_collector, **kwargs):
        """Execute this specialist's tools in its own isolated source scope."""
        from .data_sources import SourceFetcher, SourceSearcher, invoke_source
        from .plan import claim_school, school_domain
        fetcher = kwargs.pop("fetcher")
        with run.bind():
            if self.id != "internship":
                labels = list(dict.fromkeys([claim_school(claim), claim.entities.get("source_school_hint", "")]))
                for label in labels:
                    if not label:
                        continue
                    domain = invoke_source("school_catalog", lambda: school_domain(label), reference=label,
                                           action="catalog", category=claim.category)
                    if domain and kwargs.get("school_domains") is not None:
                        kwargs["school_domains"].setdefault(label, domain)
            if self.id == "competition":
                from .competitions import competition_for_claim
                invoke_source("competition_catalog", lambda: competition_for_claim(claim),
                              reference=claim.raw_text, action="catalog", category=claim.category)
            tracked_search = SourceSearcher(searcher, run)
            tracked_fetch = SourceFetcher(fetcher, run)
            if self.id == "internship":
                return manual_collector(claim, tracked_search, tracked_fetch, kwargs["budget"], kwargs.get("progress") or (lambda *_: None))
            from .strategy import github_trigger
            call = lambda: collector(claim, name, tracked_search, llm, fetcher=tracked_fetch, **kwargs)
            if self.id == "project" and github_trigger(claim):
                return invoke_source("github_records", call, reference=claim.raw_text,
                                     action="project_audit", category=claim.category)
            return call()


def _compact(value):
    return re.sub(r"[\s\W_]+", "", str(value or "")).casefold()


def _value(claim, entity_keys, element_keys):
    values = [claim.entities.get(k) for k in entity_keys]
    values += [e.partition("=")[2].strip() for e in claim.elements if e.partition("=")[0].strip() in element_keys]
    return next((v.strip() for v in values if isinstance(v, str) and v.strip()), "")


AGENTS = (
    VerificationAgent("education", "学历核验", ("学历",),
        ("学校招生/推免/毕业公示及附件", "学位论文及本人培养记录"),
        "按学校、学段和本人教育记录取证；推荐资格、录取、就读和获得学位分别核对，发布时间不是毕业年份；导师的学生学位不是导师学历。"),
    VerificationAgent("employment", "工作经历核验", ("任职",),
        ("独立第三方任职公告", "可追溯会议/协会/项目履历"),
        "分别核对单位、岗位及起止时间；受邀参加活动、合作、授课或曾为客户服务不能证明任职；只记录原文明确的职位及时间。"),
    VerificationAgent("paper", "论文核验", ("论文",),
        ("Crossref / OpenAlex 登记记录", "期刊/出版社原文"),
        "先按题名或DOI定位，再核对作者身份、刊物、年份、发表状态及位次；同题作品不能代替，预印本不等于期刊发表，共同一作与通讯作者须另取原文。"),
    VerificationAgent("book", "著作与教材核验", ("著作",),
        ("图书馆馆藏责任者", "出版社书目与版本"),
        "逐本核对题名或ISBN、本人责任者、出版方和版本年份；主编、执行主编和编著分别证明，推荐或使用教材不是作者，几本已找到的书不能证明出版总量。"),
    VerificationAgent("patent", "专利核验", ("专利",),
        ("专利公开登记", "申请/公开/授权公告"),
        "按专利号或申请号定位，再核对本人发明人、申请人、类型和法律状态；申请、公开、授权各自证明，不把申请人自动当作发明人。"),
    VerificationAgent("conference", "会议经历核验", ("会议",),
        ("主办方会议议程", "公开报告与参会记录"),
        "核对具体会议、举办年份及本人的报告/墙报/主持/参会角色；邀请不等于已参加，参会和报告不证明论文已发表，会议名称相同但年份不同不能串证据。"),
    VerificationAgent("award", "荣誉与奖学金核验", ("校内荣誉", "奖学金"),
        ("对应学校正式荣誉公示",),
        "目标奖项独立取证，核对本人、学校、奖名、年份及学段；就读、推免、申请资格、拟推荐或候选名单不能替代本人获奖。"),
    VerificationAgent("competition", "竞赛核验", ("竞赛",),
        ("赛事历年名单目录", "主办方正式赛果与名单", "对应学校参赛记录"),
        "核对具体赛事、年份/届次、国赛/省赛、组别及奖级；只确认团队赛果不能证明本人是成员，不能由成员身份推断个人贡献。"),
    VerificationAgent("internship", "实习人工背调", ("实习",),
        ("企查查对应公司页面与公开联系电话",),
        "仅整理公司查询入口和公开公司电话，供人工确认实习单位、岗位和时间；公司存在或电话可用不证明候选人实习。"),
    VerificationAgent("project", "项目贡献核验", ("项目", "开源项目"),
        ("项目独立公开记录", "本条明示的GitHub仓库/PR/commit"),
        "项目存在、参与、主导及指标分别核对；只有原文明确提及本项目仓库时才提供GitHub入口，不用数量评价能力。"),
    VerificationAgent("teaching", "教学经历核验", ("教学活动",),
        ("独立课程/活动主办方", "公开课程与教学记录"),
        "分别核对课程、授课/组织/指导角色及时间；学生学历不是导师本人学历，出版教材不证明实际授课，活动存在不能证明评价或最长等修饰语。"),
    VerificationAgent("art", "艺术成果核验", ("艺术成果",),
        ("独立主办方节目单", "公开作品/演出记录"),
        "核对具体作品、创作或演出角色及时间；组织演出不等于创作或首演，引用作品不是作者，首演等修饰语需独立材料。"),
    VerificationAgent("student_work", "学生工作核验", ("学生工作",),
        ("学生组织正式职务公示", "独立活动角色记录"),
        "核对学校/组织、本人职务和任期；活动参加者不等于组织负责人，组织存在不证明本人担任职务。"),
)


def agent_for(claim):
    return next(agent for agent in AGENTS if claim.category in agent.categories)


def extraction_instructions(claim):
    agent = agent_for(claim)
    return ("\n当前专项核验目的：" + agent.instructions
            + "\nverified_identity_anchors是其他条目已核实的导航线索，必须在本页找到本条要素的逐字原文；不能把线索复制成supports。")


def employment_record(claim, evidence):
    text = evidence.snippet or ""
    org = _value(claim, ("company", "org"), ("单位", "公司", "机构", "任职单位", "学校"))
    words = r"任职|供职|就职|入职|任命|聘任|受聘|担任|教授|讲师|研究员|工程师|经理|总监|董事|员工|工作人员|医师|教师|任教|\b(?:professor|lecturer|engineer|employed|appointed|director|manager)\b"
    # Input institution tokens alone (e.g. hosted an event at this university)
    # cannot satisfy the employer relationship.
    rows = [r for r in re.split(r"[。；;\n]", text) if re.search(words, r, re.I)]
    if org:
        from .plan import school_aliases
        names = school_aliases(org)
        rows = [r for r in rows if any(_compact(v) in _compact(r) for v in names)]
    return " ".join(rows)


def employment_supports(claim, evidence):
    """Attendance/collaboration/customer relationships don't establish a job.

    An explicit company/role biography is sufficient; it needn't say '任职'.
    Dates must be in that employment record, never borrowed from notice dates.
    """
    if claim.category != "任职":
        return evidence.supports
    body = employment_record(claim, evidence)
    if not body:
        return []
    kept = []
    for element in evidence.supports:
        field, _, value = element.partition("=")
        field, value = field.strip(), value.strip()
        if field in {"职务", "岗位", "职位", "职称"}:
            if not value or _compact(value) not in _compact(body):
                continue
            if value in {"教授", "研究员", "经理"} and re.search(r"助理" + value + r"|副" + value, body):
                # Reject a substring upgrade unless the full requested role also
                # appears explicitly as a separate title in the same record.
                if not re.search(r"(?<!助理)(?<!副)" + value, body):
                    continue
        if re.search(r"时间|年份|日期|入职|离职|起止|开始|结束", field):
            years = set(re.findall(r"(?:19|20)\d{2}", value))
            if not years or not years.issubset(set(re.findall(r"(?:19|20)\d{2}", body))):
                continue
            # A year of an event attended in that role is not the employment
            # start/end date. Require a dated job action or explicit job range.
            dated_job = re.search(r"(?:19|20)\d{2}[^，,。；;]{0,24}(?:入职|加入|受聘|获聘|任职|就职|供职|任命|离职|卸任|开始担任)|"
                                  r"(?:入职|加入|受聘|任职|就职|供职|任命|离职|卸任|任期|任职时间)[^，,。；;]{0,24}(?:19|20)\d{2}|"
                                  r"(?:19|20)\d{2}[^，,。；;]{0,12}(?:—|–|至|到)[^，,。；;]{0,12}(?:19|20)\d{2}|"
                                  r"\b(?:since|from|appointed in|joined in)\s+(?:19|20)\d{2}", body, re.I)
            if not dated_job:
                continue
        kept.append(element)
    return kept


class VerificationCoordinator:
    """Created once per verify call. No cross-account/history cache or globals."""

    def __init__(self):
        self._facts = []
        self._runs = {}
        self._batches = []
        from threading import Lock
        self._lock = Lock()

    def schedule(self, claims):
        """Only selected claims participate. Independent specialists stay parallel.

        Education stages preserve their existing order. Other specialists wait
        for selected paper/book tasks only when they can use an authorship anchor.
        Failure to prove a prerequisite doesn't prevent checking the next task.
        """
        from .academic import verification_phase
        labels = {0: "本科就读", 1: "硕士就读及入学路径", 2: "博士就读及入学路径",
                  3: "未明确学段的学历", 4: "奖项与其他经历独立取证"}
        batches = []
        for phase in sorted({verification_phase(c) for c in claims}):
            pending = [(i, c) for i, c in enumerate(claims, 1) if verification_phase(c) == phase]
            while pending:
                remaining = {agent_for(c).id for _, c in pending}
                ready = [(i, c) for i, c in pending if not set(agent_for(c).dependencies).intersection(remaining)]
                if not ready:
                    raise ValueError("核验协作计划出现循环依赖")
                batches.append((labels[phase], ready))
                ids = {c.id for _, c in ready}
                pending = [(i, c) for i, c in pending if c.id not in ids]
        self._batches = [{"order": i, "label": label, "claimIds": [c.id for _, c in group],
                          "agents": list(dict.fromkeys(agent_for(c).id for _, c in group))}
                         for i, (label, group) in enumerate(batches, 1)]
        return batches

    def start_task(self, claim, plan, budget):
        from .data_sources import AgentRun
        run = AgentRun(agent_for(claim), claim, plan, budget)
        with self._lock:
            self._runs[claim.id] = run
        return run

    def summary(self, verified):
        rows = []
        for vc in verified:
            run = self._runs.get(vc.claim.id)
            if run:
                vc.verification_agent = run.finish(vc)
                rows.append({"claimId": vc.claim.id, "agent": run.agent.describe(),
                             "sourceAccess": run.snapshot(), "handoffs": run.plan.handoffs})
        return {"version": 2, "batches": self._batches, "tasks": rows,
                "handoffs": [h for row in rows for h in row["handoffs"]],
                "confirmedAnchors": deepcopy(self._facts),
                "scope": "本次报告内协作；随报告保存，按现有账户历史隔离。",
                "note": "共享线索与目录只用于定位；每条经历须分别取得本人的原文证据，最终状态由规则判定。"}

    def context_for(self, claim):
        entities = deepcopy(claim.entities)
        entities["verified_identity_anchors"] = deepcopy(self._facts)
        return claim.model_copy(update={"entities": entities})

    def record(self, verified):
        from .source_guard import evidence_excluded, claim_sources
        allowed_fields = {"学校", "专业", "专业/方向", "单位", "公司", "机构", "任职单位",
                          "论文标题", "论文题目", "书名", "著作名称", "DOI", "ISBN"}
        for vc in verified:
            if vc.status not in {"ok", "part"} or vc.claim.category not in {"学历", "任职", "论文", "著作"}:
                continue
            for element in vc.proved:
                if element.partition("=")[0].strip() not in allowed_fields:
                    continue
                urls = list(dict.fromkeys(e.url for e in vc.evidence if
                    element in e.supports and e.identity_score >= 2
                    and not e.identity_conflicts and e.extraction_method == "text"
                    and not evidence_excluded(e, claim_sources(vc.claim))))
                if not urls:
                    continue
                fact = {"claimId": vc.claim.id, "category": vc.claim.category,
                        "element": element, "stage": "", "urls": urls}
                if vc.claim.category == "学历":
                    from .academic import study_stage
                    fact["stage"] = study_stage(vc.claim)
                if fact not in self._facts:
                    self._facts.append(fact)

"""教师学历渠道与阶段准入回归，测试姓名及名单内容均为合成数据。"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.collect import (_education_year_queries, _official_roster_links,
                         _read_and_extract, extract_evidence, _relevance, Budget)
from app.identity import education_record_relevant, verbatim_excerpt
from app.identity import with_candidate_identity_context
from app.education import faculty_training_pages
from app.schema import Claim, Evidence
from app.search import SearchHit
from app.strategy import ResumeProfile, build_plan


def degree():
    return Claim(id="degree", raw_text="华南理工大学 管理科学与工程博士",
                 raw_locator="测试", category="学历", date_label="",
                 elements=["学校=华南理工大学", "专业=管理科学与工程", "学位=博士"],
                 entities={"org": "华南理工大学"})


class TeacherEducationTests(unittest.TestCase):
    def test_no_year_teacher_has_training_routes_and_keeps_1980_window(self):
        claim = degree()
        plan = build_plan(claim, "李明", ResumeProfile(identity="researcher"))
        self.assertTrue(any("博士" in q.text for q in plan.queries))
        self.assertTrue(any(q.source == "education_route:硕博连读" for q in plan.queries))
        self.assertTrue(any("1980" in q.text for q in plan.queries))
        self.assertEqual(claim.date_label, "")
        self.assertNotIn("入学方式=推免", claim.elements)
        student = build_plan(claim, "李明", ResumeProfile(identity="student"))
        self.assertFalse(any(q.source.startswith("education_route:") for q in student.queries))

    def test_journal_name_and_school_are_not_education_evidence(self):
        text = "李明, 王清. 道路优化[J]. 华南理工大学学报, 2024."
        llm = SimpleNamespace(complete_json=lambda *a: {
            "snippet": text, "supports": ["学校=华南理工大学"],
            "identity_signals": ["学校一致"]})
        self.assertIsNone(extract_evidence(degree(), SearchHit("https://zrb.scut.edu.cn/a", "论文"),
                                          text, llm, candidate_name="李明"))
        self.assertFalse(education_record_relevant(text, "期刊论文"))
        roster = SearchHit("https://yz.scut.edu.cn/list.pdf", "2021年推免名单")
        paper = SearchHit("https://zrb.scut.edu.cn/a", "华南理工大学学报道路研究", text)
        self.assertGreater(_relevance(roster, "李明", degree()), _relevance(paper, "李明", degree()))

    def test_admission_and_training_do_not_prove_awarded_doctorate(self):
        for text in ("2022级博士生 李明 管理科学与工程专业1+4硕博连读。",
                     "2021年推免拟录取名单：李明 管理科学与工程 博士。"):
            llm = SimpleNamespace(complete_json=lambda *a: {
                "snippet": text, "supports": degree().elements,
                "identity_signals": ["学校一致", "专业一致"]})
            ev = extract_evidence(degree(), SearchHit("https://cnsba.scut.edu.cn/a", "培养记录"),
                                  text, llm, candidate_name="李明")
            self.assertIsNotNone(ev)
            self.assertNotIn("学位=博士", ev.supports)
            self.assertIn("学校=华南理工大学", ev.supports)
        text = "李明于2026年获得华南理工大学管理科学与工程博士学位。"
        llm = SimpleNamespace(complete_json=lambda *a: {
            "snippet": text, "supports": degree().elements, "identity_signals": ["学校一致"]})
        self.assertIn("学位=博士", extract_evidence(degree(), SearchHit("https://scut.edu.cn/a"),
                      text, llm, candidate_name="李明").supports)

    def test_no_push_word_in_claim_still_follows_official_attachment(self):
        notice = SearchHit("https://yz.scut.edu.cn/notice.htm", "2021年接收推免名单公示")
        pdf = "https://yz.scut.edu.cn/list.pdf"
        html = '<main>名单见附件<a href="list.pdf">推免名单.pdf</a></main>'
        self.assertEqual(_official_roster_links(html, notice, degree())[0].url, pdf)
        content = {notice.url: html, pdf: "2021年直博推免名单\n李明 工商管理学院 管理科学与工程 博士"}
        calls = []
        llm = SimpleNamespace(complete_json=lambda *a: calls.append(a) or {
            "snippet": "李明 工商管理学院 管理科学与工程 博士", "supports": ["学校=华南理工大学"],
            "identity_signals": ["学校一致", "专业一致"]})
        budget = Budget(max_searches=0, max_page_reads=2)
        budget.note_read()
        found = _read_and_extract(degree(), [notice], SimpleNamespace(get=lambda u: content[u]),
                                  llm, set(), candidate_name="李明", budget=budget)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.page_reads, 2)

    def test_source_year_not_page_date_drives_admission_followup(self):
        ev = Evidence(url="https://cnsba.scut.edu.cn/a", title="导师主页", publisher="华南理工大学",
                      source_tier="B", accessed_at="2026-10-09", published_at="2026-10-09",
                      snippet="2021级硕士 […] 李明 工业工程专业保研", identity_score=3,
                      identity_signals=["学校一致", "专业一致"], supports=["学校=华南理工大学"])
        probes = _education_year_queries(degree(), ev, "李明", "scut.edu.cn")
        self.assertEqual([q.text for q in probes], ["2021 推免 拟录取 名单"])
        ev.snippet = "2022级博士生 李明 管理科学与工程专业1+4硕博连读"
        self.assertEqual([q.text for q in _education_year_queries(degree(), ev, "李明", "scut.edu.cn")],
                         ["2022 硕博连读 博士 拟录取 名单"])
        ev.snippet = "2021级硕士 […] 李明 工业工程专业保研"
        ev.extraction_method = "search_snippet"
        self.assertEqual(_education_year_queries(degree(), ev, "李明", "scut.edu.cn"), [])
        ev.extraction_method = "text"
        ev.snippet = "李明在华南理工大学学习。"
        self.assertEqual(_education_year_queries(degree(), ev, "李明", "scut.edu.cn"), [])

    def test_explicit_omission_preserves_original_order_and_text(self):
        self.assertTrue(verbatim_excerpt("2021级硕士 […] 李明 工业工程专业保研",
                                        "2021级硕士\n张清 数学专业\n李明 工业工程专业保研"))
        self.assertFalse(verbatim_excerpt("2022级硕士 […] 李明 工业工程专业保研",
                                         "2021级硕士\n李明 工业工程专业保研"))
        self.assertFalse(verbatim_excerpt("李明 […] 2021级硕士", "2021级硕士 李明"))

    def test_directory_uses_observed_colleague_links_in_bounded_reads(self):
        home, directory, teacher = ("https://dept.scut.edu.cn/", "https://dept.scut.edu.cn/faculty",
                                    "https://faculty.scut.edu.cn/zhang/main.htm")
        data = {home: '<nav>师资队伍<a href="faculty">师资力量</a></nav>',
                directory: '<a href="https://evil.example/a"><h2>王清</h2></a>'
                           f'<a href="{teacher}"><h2>张清</h2></a>'
                           '<a href="https://faculty.scut.edu.cn/other"><h2>刘明</h2></a>',
                teacher: '2021级硕士 李明 工业工程专业保研'}
        reads = []
        with patch("app.education._school_record", return_value={"colleges": [["工商管理学院", home]]}):
            pages = faculty_training_pages(degree(), "李明", "scut.edu.cn",
                    lambda u: reads.append(u) or data[u], select=lambda ps: ["张清", "王清"], max_reads=3)
        self.assertEqual(reads, [home, directory, teacher])
        self.assertEqual([p.url for p in pages], [teacher])
        self.assertEqual(pages[0].content, data[teacher])

    def test_paper_coauthors_survive_selection_as_retrieval_hints(self):
        paper = Claim(id="paper", category="论文", raw_text="A paper. (With Baozhuang Niu, Nan Zhang)",
                      raw_locator="测试", date_label="", elements=[])
        context = with_candidate_identity_context([degree(), paper])[0].entities["candidate_identity_context"]
        self.assertEqual(context["research_collaborators"], ["Baozhuang Niu", "Nan Zhang"])
        self.assertEqual(context["education_fields"][0]["field"], "管理科学与工程")

    def test_push_origin_in_supervisor_list_counts_as_undergraduate_source(self):
        from app.academic import academic_supports, education_scope_matches
        bachelor = Claim(id="ba", raw_text="华南理工大学 工业工程 本科", raw_locator="测试",
                         category="学历", date_label="2017-2021",
                         elements=["学校=华南理工大学", "专业=工业工程", "学位=本科", "时间=2017-2021"],
                         entities={"org": "华南理工大学"})
        title = "导师 · 教师个人主页"
        origin = "2021级硕士 李明 工业工程专业保研（华工工管学院）"
        self.assertTrue(education_scope_matches(bachelor, origin, title))
        ev = Evidence(url="https://faculty.scut.edu.cn/a", title=title, snippet=origin,
                      publisher="华南理工大学", source_tier="A",
                      supports=list(bachelor.elements))
        self.assertEqual(academic_supports(bachelor, ev), ["学校=华南理工大学", "专业=工业工程"])
        # 硕博连读/考博括号里是硕士来源，不能当本科
        for text in ("2022级博士生 李明 管理工程专业1+4硕博连读（华工工管学院）",
                     "李明 物流工程专业考博（西南交大交通物流学院）"):
            self.assertFalse(education_scope_matches(bachelor, text, title))
        # 硕士条目不受影响
        self.assertTrue(education_scope_matches(degree().model_copy(update={
            "raw_text": "华南理工大学 工业工程硕士", "elements": ["学校=华南理工大学", "学位=硕士"]}),
            origin, title))


if __name__ == "__main__":
    unittest.main()

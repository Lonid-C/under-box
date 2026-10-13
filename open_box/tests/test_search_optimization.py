"""Synthetic search/metadata controls. No paid requests or real resumes."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from app import judge
from app.collect import Budget, _read_and_extract, extract_evidence
from app.paper_identity import author_match, from_hit, supports_for
from app.public_documents import legacy_office_text
from app.schema import Claim, Evidence
from app.search import SearchHit, _crossref_hit, paper_hits
from app.strategy import ResumeProfile, build_plan
from app.verification_agents import teaching_supports

TITLE = "Synthetic Sparse Reconstruction"
DOI = "10.9999/synthetic-test"

def paper():
    return Claim(id="p", category="论文", raw_text="Chen Y., Li B. " + TITLE + ". Synthetic Journal.",
        raw_locator="测试", date_label="2024", entities={"candidate_name":"陈一", "title":TITLE,
        "venue":"Synthetic Journal", "candidate_identity_context":{"candidate_name":"陈一",
        "education_schools":["哈尔滨工业大学"], "current_employers":[]}},
        elements=["论文标题="+TITLE, "刊物=Synthetic Journal", "刊物类型=期刊", "作者位次=第一作者",
                  "年份=2024", "收录=JCR Q1", "影响因子=7.8"])

def hit():
    return SearchHit("https://doi.org/"+DOI, TITLE, venue="Synthetic Journal", year="2024",
        content=TITLE, publication_type="journal-article", registry_sources=("https://api.crossref.org/works/"+DOI,),
        author_records=({"name":"Yi Chen","given":"Yi","family":"Chen","position":1,
                         "institutions":["哈尔滨工业大学"]},
                        {"name":"Bo Li","given":"Bo","family":"Li","position":2,"institutions":[]}))

class PaperIdentityTests(unittest.TestCase):
    def test_full_name_own_affiliation_and_coauthor_joint_match_stays_manual(self):
        c = paper(); ev = from_hit(c, hit()); self.assertIsNotNone(ev)
        result = judge.assess(c, [ev])
        self.assertEqual(result.proved, c.elements[:5]); self.assertEqual(result.status,"part")
        self.assertTrue(result.needs_human); self.assertEqual(result.evidence[0].source_tier,"C")

    def test_name_only_other_author_affiliation_and_wrong_institution_rejected(self):
        for affiliation in ([], ["另一个学校"]):
            h = hit(); rows = copy.deepcopy(list(h.author_records))
            rows[0]["institutions"] = affiliation; rows[1]["institutions"] = ["哈尔滨工业大学"]
            h.author_records = tuple(rows)
            self.assertIsNone(from_hit(paper(), h))

    def test_initials_alone_and_duplicate_full_names_not_unique(self):
        for name in ("Y. Chen", "Yi Chen"):
            h = hit(); rows = copy.deepcopy(list(h.author_records)); rows[0]["name"] = name
            if name == "Yi Chen": rows.append(copy.deepcopy(rows[0]))
            h.author_records = tuple(rows); self.assertIsNone(from_hit(paper(), h))

    def test_chinese_pinyin_requires_original_citation_and_coauthor(self):
        for raw in (TITLE + ". Synthetic Journal.", "Chen Y. " + TITLE + ". Synthetic Journal."):
            c=paper(); c.raw_text=raw; self.assertIsNone(from_hit(c,hit()))

    def test_exact_title_venue_doi_and_registry_required(self):
        for field, value in (("title",TITLE+" Extended"),("venue","Another Journal"),
                             ("registry_sources",("https://api.crossref.org.evil.test/works/"+DOI,))):
            h=hit(); setattr(h,field,value); self.assertIsNone(from_hit(paper(),h))
        c=paper(); c.entities["doi"]="10.9999/another"; self.assertIsNone(from_hit(c,hit()))

    def test_cannot_upgrade_first_author_to_cofirst_or_prove_jcr_and_if(self):
        c=paper(); c.elements += ["作者位次=共同第一作者","学校=哈尔滨工业大学","贡献=主导研究"]
        self.assertEqual(supports_for(c,from_hit(c,hit()).paper_record),c.elements[:5])

    def test_persisted_score_and_supports_are_recomputed(self):
        c=paper(); e=from_hit(c,hit()); e.supports=c.elements; e.source_tier="A"; e.identity_score=12
        result=judge.assess(c,[e],rescore=False)
        self.assertEqual(result.proved,c.elements[:5]); self.assertEqual(result.status,"part")
        self.assertLessEqual(result.evidence[0].identity_score,3)

    def test_malformed_metadata_does_not_crash_or_admit(self):
        for rows in ([None], ["Yi Chen"], [{"name":"Yi Chen","institutions":"哈尔滨工业大学"}]):
            e=from_hit(paper(),hit()); e.paper_record["authors"]=rows
            self.assertFalse(author_match(paper(),e.paper_record).allowed)
        record=from_hit(paper(),hit()).paper_record; record["sources"]=[None]
        self.assertFalse(author_match(paper(),record).allowed)

    def test_ror_domain_association_belongs_to_candidate_author(self):
        h=hit(); rows=copy.deepcopy(list(h.author_records)); rows[0]["institutions"]=["Harbin Institute of Technology"]
        rows[0]["institution_records"]=[{"ror":"https://ror.org/01yqg2h08","domains":["hit.edu.cn"],
            "source":"https://api.ror.org/v2/organizations/01yqg2h08"}]
        h.author_records=tuple(rows); self.assertIsNotNone(from_hit(paper(),h))
        rows[0]["institution_records"][0]["domains"]=["hit.edu.cn.evil.test"]
        self.assertIsNone(from_hit(paper(),h))

    def test_merge_preserves_matched_author_affiliations_not_other_authors(self):
        cross=hit(); cross.author_records=tuple({**r,"institutions":[]} for r in cross.author_records)
        other=hit(); other.registry_sources=("https://api.openalex.org/works/W123",)
        with patch("app.search.crossref_hits",return_value=[cross]), patch("app.search.openalex_hits",return_value=[other]):
            rows=paper_hits(TITLE,author="陈一",venue="Synthetic Journal")
        self.assertEqual(rows[0].author_records[0]["institutions"],["哈尔滨工业大学"])
        self.assertEqual(rows[0].author_records[1]["institutions"],[])
        self.assertEqual(rows[0].author_records[0]["position"],1)
        self.assertEqual(len(rows[0].registry_sources),2)
        e=from_hit(paper(),rows[0])
        self.assertTrue(all(u in e.snippet for u in rows[0].registry_sources))

    def test_conflicting_title_same_doi_does_not_merge(self):
        cross=hit(); cross.author_records=tuple({**r,"institutions":[]} for r in cross.author_records)
        other=hit(); other.title=TITLE+" Extended"
        with patch("app.search.crossref_hits",return_value=[cross]), patch("app.search.openalex_hits",return_value=[other]):
            rows=paper_hits(TITLE,author="陈一")
        self.assertEqual(rows[0].author_records[0]["institutions"],[])

    def test_metadata_collect_path_uses_rules_without_model_invention(self):
        class Never:
            def complete_json(self,*a,**k): raise AssertionError("structured records use no model")
        e=extract_evidence(paper(),hit(),TITLE,Never(),candidate_name="陈一")
        self.assertIsNotNone(e); self.assertEqual(e.supports,paper().elements[:5])

class TeachingTests(unittest.TestCase):
    def setUp(self):
        self.c=Claim(id="t",category="教学活动",raw_text="陈一教授承担本科公共课和研究生课程，研究生导师",
            raw_locator="测试",date_label="",entities={"candidate_name":"陈一"},
            elements=["课程=视唱练耳公共课","授课对象=本科","授课对象=研究生","role=教师","role=研究生导师"])
    def supports(self,text):
        e=Evidence(url="https://independent.example/t",title="教学",publisher="",source_tier="B",
                   snippet=text,supports=self.c.elements)
        return teaching_supports(self.c,e)
    def test_supervisor_and_generic_professional_students_not_course_objects(self):
        got=self.supports("陈一，硕士生导师，教授专业生课程及全院视唱练耳共同课。")
        self.assertEqual(got,[self.c.elements[0],*self.c.elements[3:]])
    def test_explicit_undergraduate_and_graduate_courses_are_supported(self):
        got=self.supports("陈一教授视唱练耳公共课，面向本科生和研究生授课，硕士生导师。")
        self.assertEqual(got,self.c.elements)
    def test_other_person_supervision_not_borrowed(self):
        self.assertEqual(self.supports("陈一的导师王明教授本科课程，是硕士生导师。"),[])
    def test_unattributed_next_sentence_not_borrowed(self):
        self.assertEqual(self.supports("陈一，硕士生导师。王明教授本科课程。"),["role=研究生导师"])
    def test_undergraduate_course_does_not_imply_graduate_course(self):
        got=self.supports("陈一讲授本科视唱练耳公共课，硕士生导师。")
        self.assertNotIn("授课对象=研究生",got); self.assertIn("授课对象=本科",got)
    def test_halfwidth_punctuation_and_optional_student_suffix_not_supervisor_courses(self):
        for role in ("硕士导师","硕士生导师","硕士研究生导师","博士生导师"):
            got=self.supports("陈一,作曲系副教授,"+role+"｡教授专业生课程及全院视唱练耳共同课｡")
            self.assertNotIn("授课对象=研究生",got); self.assertNotIn("授课对象=本科",got)

class PublicDocumentTests(unittest.TestCase):
    def test_real_binary_xls_preserves_table_headers_and_candidate_row(self):
        data=(Path(__file__).parent/"fixtures/synthetic-roster.xls").read_bytes()
        text=legacy_office_text(data)
        self.assertIn("姓名 | 学校 | 学段 | 年份",text)
        self.assertIn("合成学生 | 合成大学 | 硕士 | 2021",text)
        self.assertNotIn("2021.0",text)
    def test_non_office_and_oversized_input_not_parsed(self):
        self.assertIsNone(legacy_office_text(b"not office"))
        self.assertIsNone(legacy_office_text(b"\xd0\xcf\x11\xe0"+b"x"*(12*1024*1024)))
    def test_missing_doc_reader_is_read_failure_not_gibberish(self):
        with patch("app.public_documents.shutil.which",return_value=None):
            self.assertIsNone(legacy_office_text(b"\xd0\xcf\x11\xe0"+b"x"*30))
    def test_zip_attachment_reads_legacy_xls_without_extracting_member_paths(self):
        import io,zipfile
        from app.roster_archive import archive_text
        data=(Path(__file__).parent/"fixtures/synthetic-roster.xls").read_bytes()
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w") as z:
            z.writestr("2021硕士名单.xls",data); z.writestr("../bad.xls",data)
        text=archive_text(out.getvalue(),pdf_parser=lambda _:None,office_parser=lambda _:None)
        self.assertIn("2021硕士名单.xls",text); self.assertIn("合成学生",text); self.assertNotIn("bad.xls",text)

class RecoveryTests(unittest.TestCase):
    OLD="https://www.ccom.edu.cn/info/100/old.htm"
    NEW="https://www.ccom.edu.cn/info/200/new.htm"
    def run_recovery(self,reason="HTTP 404",budget=None,replacement=None):
        c=Claim(id="r",category="教学活动",raw_text="陈一研究生导师",raw_locator="",date_label="",
                entities={"candidate_name":"陈一"},elements=["role=研究生导师"])
        old=SearchHit(self.OLD,"合成学院研究生导师答辩公告",snippet="陈一研究生导师")
        new=replacement or SearchHit(self.NEW,old.title)
        class Fetch:
            failures={RecoveryTests.OLD:reason}; resolved_urls={}
            def __init__(self): self.calls=[]
            def get(self,u): self.calls.append(u); return "陈一研究生导师" if u==RecoveryTests.NEW else None
        class Search:
            def __init__(self): self.calls=[]
            def search(self,q,site=None):
                self.calls.append((q,site))
                if isinstance(new,Exception): raise new
                return [new]
        class LLM:
            def complete_json(self,*a,**k): return {"snippet":"陈一研究生导师","supports":c.elements}
        fetch,search=Fetch(),Search(); b=budget or Budget(max_searches=2,max_page_reads=2)
        ev=_read_and_extract(c,[old],fetch,LLM(),set(),candidate_name="陈一",budget=b,searcher=search)
        return ev,fetch,search,b
    def test_only_observed_same_school_new_url_read_with_budget(self):
        ev,f,s,b=self.run_recovery()
        self.assertEqual(s.calls,[("\"合成学院研究生导师答辩公告\"","ccom.edu.cn")])
        self.assertIn(self.NEW,f.calls); self.assertTrue(any(e.url==self.NEW and e.extraction_method=="text" for e in ev))
        self.assertEqual((b.searches,b.page_reads),(1,1))
    def test_robots_ssl_and_403_do_not_trigger_recovery(self):
        for reason in ("robots.txt 不允许访问","证书校验失败（SSL）","HTTP 403"):
            ev,f,s,b=self.run_recovery(reason); self.assertEqual(s.calls,[])
            self.assertNotIn(self.NEW,f.calls)
    def test_cross_domain_and_different_title_results_not_read(self):
        for new in (SearchHit("https://ccom.edu.cn.evil.test/new","合成学院研究生导师答辩公告"),
                    SearchHit(self.NEW,"不相关人物活动")):
            _,f,s,b=self.run_recovery(replacement=new); self.assertNotIn(new.url,f.calls)
    def test_no_budget_means_no_recovery(self):
        _,f,s,b=self.run_recovery(budget=Budget(max_searches=0)); self.assertEqual(s.calls,[])
        from app.search import SearchFiltered
        ev,f,s,b=self.run_recovery(replacement=SearchFiltered("synthetic filtered title"))
        self.assertEqual(len(s.calls),1); self.assertNotIn(self.NEW,f.calls)
        self.assertTrue(all(e.extraction_method=="search_snippet" for e in ev))

class NavigationTests(unittest.TestCase):
    def test_explicit_graduation_year_is_only_a_probe_with_fixed_budget(self):
        c=Claim(id="n",category="学历",raw_text="2019-2023哈尔滨工程大学计算机本科",raw_locator="",date_label="2019-2023",
            date_start="2019-09",date_end="2023-06",entities={"org":"哈尔滨工程大学"},
            elements=["学校=哈尔滨工程大学","学段=本科"])
        p=build_plan(c,"合成学生",ResumeProfile(identity="student"))
        self.assertTrue(any(q.text=='"合成学生" 2023 毕业' for q in p.queries))
        self.assertEqual(c.elements,["学校=哈尔滨工程大学","学段=本科"])
        self.assertEqual(p.budget["searches"],8)  # Existing student profile budget unchanged.

if __name__ == "__main__": unittest.main()

"""人员身份准入回归：名字、时间、来源域名和模型分数不能冒充本人证据。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.schema import Claim,Evidence
from app.identity import with_candidate_identity_context
from app.person_scope import check_person
from app.collect import extract_evidence,_read_and_extract
from app.search import SearchHit
from app import judge,pipeline
from app.parse import ParsedDocument
from app.strategy import ResumeProfile

NOTICE='https://gs.xmu.edu.cn/info/1471/55602.htm'
TITLE='地球科学与技术学部拟聘决议（20250714）'
ROW='海洋与地球学院：助理教授 唐思'
SCHOOL='香港中文大学（深圳）'

def student():
 return Claim(id='ba',category='学历',raw_text='2023.09-至今 香港中文大学（深圳）音乐学院 钢琴表演 本科',raw_locator='',date_label='2023.09-至今',date_start='2023-09-01',elements=['学校='+SCHOOL,'专业=钢琴表演','学位=本科'],entities={'org':SCHOOL,'major':'钢琴表演','dept':'音乐学院'})
def work():
 return Claim(id='work',category='学生工作',raw_text='2024.05 2024祥波书院毕业晚宴 执行团队志愿者',raw_locator='',date_label='2024.05',elements=['组织=2024祥波书院毕业晚宴','岗位=执行团队志愿者'],entities={'org':'2024祥波书院毕业晚宴'})
def contextual(claim=None):return with_candidate_identity_context([student(),claim or work()],'唐思')[-1]
def ev(text=ROW,**kw):
 data=dict(url=NOTICE,title=TITLE,publisher='厦门大学研究生院',source_tier='D',snippet=text,published_at='2025-07-14',identity_signals=['时间吻合'],supports=[]);data.update(kw);return Evidence(**data)
class NoLLM:
 def complete_json(self,*a,**k):raise AssertionError('错配人员不应送给 LLM 抽取')

class PersonScopeTests(unittest.TestCase):
 def test_exact_user_counterexample_rejected_before_model(self):
  c=contextual();self.assertFalse(check_person(c,NOTICE,ROW,TITLE).allowed)
  self.assertIsNone(extract_evidence(c,SearchHit(NOTICE,TITLE),ROW,NoLLM(),candidate_name='唐思'))
 def test_name_not_matched_as_suffix_of_another_person(self):
  c=contextual()
  self.assertFalse(check_person(c,'https://cuhk.edu.cn/a',SCHOOL+'本科生 王唐思','2024晚宴').allowed)
 def test_doctoral_student_mentioned_as_supervisor_not_candidates_stage(self):
  c=contextual();text=SCHOOL+'本科生唐思，在博士生李明指导下担任2024祥波书院毕业晚宴志愿者'
  self.assertTrue(check_person(c,'https://cuhk.edu.cn/a',text,'2024年晚宴').allowed)
 def test_model_hallucinated_stage_does_not_override_explicit_bachelor_text(self):
  ba=student().model_copy(update={'elements':['学校='+SCHOOL,'学位=博士']})
  c=with_candidate_identity_context([ba,work()],'唐思')[-1]
  self.assertEqual(c.entities['candidate_identity_context']['education_records'][0]['stage'],'本科')
  self.assertFalse(check_person(c,NOTICE,ROW,TITLE).allowed)
 def test_hallucinated_claim_organization_and_title_cannot_enable_match(self):
  c=work().model_copy(update={'entities':{'org':'厦门大学','title':'地球科学与技术学部拟聘决议'}})
  c=with_candidate_identity_context([student(),c],'唐思')[-1]
  self.assertFalse(check_person(c,NOTICE,'厦门大学学生 唐思',TITLE).allowed)
 def test_time_only_record_not_in_evidence_even_with_forged_score(self):
  c=contextual();bad=ev(identity_score=99,identity_signals=['学校一致','专业一致','时间吻合'],supports=c.elements)
  for rescore in (True,False):
   report=judge.assess(c,[bad],rescore=rescore)
   self.assertEqual(report.evidence,[]);self.assertEqual(report.proved,[]);self.assertEqual(report.status,'none')
 def test_same_school_staff_record_still_not_student(self):
  c=contextual();text=SCHOOL+'音乐学院：助理教授 唐思'
  self.assertFalse(check_person(c,'https://cuhk.edu.cn/notice',text,'2025年拟聘教师').allowed)
 def test_professor_mentioned_as_students_supervisor_not_students_role(self):
  c=contextual();text=SCHOOL+'本科生唐思，在助理教授李明指导下担任2024祥波书院毕业晚宴执行团队志愿者'
  self.assertTrue(check_person(c,'https://cuhk.edu.cn/news',text,'2024年晚宴').allowed)
 def test_name_and_page_date_without_any_identity_anchor_not_evidence(self):
  c=contextual()
  self.assertFalse(check_person(c,'https://news.example/a','唐思参加了一项活动。','2024年新闻',published_at='2024-05-01').allowed)
  self.assertEqual(judge.assess(c,[ev('唐思参加了一项活动',published_at='2024-05-01')]).evidence,[])
 def test_different_name_not_matched_as_prefix(self):
  c=contextual()
  self.assertFalse(check_person(c,'https://cuhk.edu.cn/news',SCHOOL+'本科生 唐思远 钢琴表演','2024年晚宴').allowed)
 def test_footer_cannot_supply_persons_school(self):
  c=contextual();body=ROW+'\n\n友情链接 香港中文大学（深圳） 音乐学院'
  self.assertFalse(check_person(c,NOTICE,body,TITLE,final=False).allowed)
  generic='唐思参与活动\n\n版权所有 香港中文大学（深圳）'
  self.assertFalse(check_person(c,'https://other.edu.cn/a',generic,'活动').allowed)
 def test_wrong_students_record_cannot_borrow_neighbours_school(self):
  c=contextual();body=SCHOOL+'2023级本科生 李明 钢琴表演\n厦门大学 海洋与地球学院 唐思'
  self.assertFalse(check_person(c,NOTICE,body,'学生名单').allowed)
 def test_only_correct_person_row_from_mixed_page_can_be_extracted(self):
  c=contextual();good=SCHOOL+'本科生 唐思 2024祥波书院毕业晚宴执行团队志愿者'
  body=good+'\n'+ROW
  self.assertTrue(check_person(c,NOTICE,body,TITLE,final=False).allowed)
  self.assertTrue(check_person(c,NOTICE,good,'2024年晚宴').allowed)
  self.assertFalse(check_person(c,NOTICE,good+'\n'+ROW,TITLE).allowed)
 def test_correct_student_experience_kept_and_model_school_signal_replaced(self):
  c=contextual();text=SCHOOL+'本科生 唐思担任2024祥波书院毕业晚宴执行团队志愿者'
  llm=SimpleNamespace(complete_json=lambda *a,**k:{'snippet':text,'supports':c.elements,'identity_signals':['年级一致','任职单位一致','时间吻合']})
  found=extract_evidence(c,SearchHit('https://www.cuhk.edu.cn/news',title='2024祥波书院毕业晚宴'),text,llm,candidate_name='唐思')
  self.assertIsNotNone(found);self.assertIn('学校一致',found.identity_signals)
  self.assertNotIn('年级一致',found.identity_signals);self.assertNotIn('任职单位一致',found.identity_signals)
  self.assertTrue(judge.assess(c,[found]).evidence)
 def test_wrong_record_exclusion_logged(self):
  c=contextual();logs=[]
  found=_read_and_extract(c,[SearchHit(NOTICE,TITLE,content=ROW)],SimpleNamespace(get=lambda u:ROW),NoLLM(),set(),candidate_name='唐思',progress=logs.append)
  self.assertEqual(found,[]);self.assertTrue(any('排除未能关联本人的来源' in s for s in logs))
 def test_known_same_school_targeted_student_roster_kept(self):
  c=contextual();text='2023级本科生 唐思 钢琴表演 执行团队志愿者'
  checked=check_person(c,'https://www.cuhk.edu.cn/news/1',text,'2024祥波书院毕业晚宴')
  self.assertTrue(checked.allowed);self.assertIn('学校一致',checked.signals)
 def test_same_school_domain_name_alone_is_not_identity_proof(self):
  c=contextual()
  self.assertFalse(check_person(c,'https://cuhk.edu.cn/a','唐思','公告').allowed)
 def test_current_teacher_known_education_record_remains_usable(self):
  teacher=Claim(id='job',category='任职',raw_text='目前任职 BNBU 助理教授',raw_locator='',date_label='',entities={'org':'BNBU','role':'助理教授'})
  degree=Claim(id='ms',category='学历',raw_text='四川大学工业工程硕士',raw_locator='',date_label='',entities={'org':'四川大学'},elements=['学校=四川大学','学位=硕士'])
  c=with_candidate_identity_context([teacher,degree],'廖志强')[-1]
  self.assertTrue(check_person(c,'https://scu.edu.cn/roster','2021级硕士 廖志强 工业工程','四川大学录取名单').allowed)
  self.assertFalse(check_person(c,'https://water.example/a','水利局 副局长 廖志强','任免公告').allowed)
 def test_current_undergrad_cannot_match_contemporaneous_doctoral_student(self):
  c=contextual()
  self.assertFalse(check_person(c,'https://cuhk.edu.cn/a',SCHOOL+'2024级博士生 唐思','2025年在读生名单').allowed)
 def test_recommendation_is_not_confused_with_current_graduate_enrollment(self):
  c=contextual()
  self.assertTrue(check_person(c,'https://cuhk.edu.cn/a',SCHOOL+'本科生 唐思 获2026年免试攻读硕士资格','2025年推荐名单').allowed)
 def test_professional_taking_second_bachelor_not_assumed_impossible(self):
  faculty=Claim(id='job',category='任职',raw_text='现任音乐学院助理教授',raw_locator='',date_label='',entities={'org':SCHOOL,'role':'助理教授'})
  c=with_candidate_identity_context([student(),faculty],'唐思')[-1]
  self.assertTrue(check_person(c,'https://cuhk.edu.cn/a',SCHOOL+'助理教授 唐思','2025年教师聘任').allowed)
 def test_historic_study_does_not_make_all_future_faculty_records_conflicts(self):
  ba=student().model_copy(update={'raw_text':'2010-2014 '+SCHOOL+'本科','date_label':'2010-2014','date_start':'2010-09-01','date_end':'2014-07-01'})
  c=with_candidate_identity_context([ba,work()],'唐思')[-1]
  self.assertTrue(check_person(c,'https://cuhk.edu.cn/a',SCHOOL+'助理教授 唐思','2025年教师聘任').allowed)
 def test_same_paper_title_cannot_override_explicit_foreign_affiliation(self):
  c=Claim(id='p',category='论文',raw_text='Numerical study of acoustic tides',raw_locator='',date_label='2020',elements=['论文标题=Numerical study of acoustic tides'],entities={'title':'Numerical study of acoustic tides'})
  c=with_candidate_identity_context([student(),c],'唐思')[-1]
  checked=check_person(c,'https://publisher.example/a','唐思 厦门大学. Numerical study of acoustic tides','论文')
  self.assertFalse(checked.allowed)
  linked=check_person(c,'https://publisher.example/a','唐思 香港中文大学（深圳）曾就读厦门大学. Numerical study of acoustic tides','论文')
  self.assertTrue(linked.allowed)
 def test_input_links_and_model_promises_cannot_override_wrong_identity(self):
  c=contextual();c.entities['provided_urls']=[NOTICE]
  self.assertIsNone(extract_evidence(c,SearchHit(NOTICE,TITLE),ROW,NoLLM(),candidate_name='唐思',candidate_provided=True))
 def test_unselected_degree_used_when_only_student_work_checked(self):
  prepared=pipeline.PreparedResume('synthetic',ParsedDocument('合成学生简历',[],'synthetic'),NoLLM(),ResumeProfile(identity='student',name='唐思'),[student(),work()],'唐思')
  def fake_collect(c,*a,**k):
   self.assertEqual(c.entities['candidate_identity_context']['education_records'][0]['stage'],'本科')
   return [ev(supports=c.elements,identity_signals=['学校一致','时间吻合'])],False
  with patch.object(pipeline,'collect_for_claim',fake_collect),patch.object(pipeline,'generate_question',return_value='补证'):
   result=pipeline.verify_prepared(prepared,claim_ids=['work'],searcher=object(),fetcher=object())
  self.assertEqual(len(result.claims),1);self.assertEqual(result.claims[0].evidence,[])

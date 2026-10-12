import unittest
from unittest.mock import patch

from app import judge,pipeline
from app.schema import Claim,Evidence
from app.faculty_recall import enrich_input,authored_works,book_supports
from app.identity import with_candidate_identity_context
from app.person_scope import check_person
from app.strategy import build_plan,ResumeProfile
from app.source_guard import policy,for_claim,claim_sources,excluded
from app.collect import extract_evidence
from app.search import SearchHit
from app.book_catalog import pdf_catalog_text

BIO='付妮，现任香港中文大学（深圳）音乐学院教授，毕业于中央音乐学院视唱练耳专业并留校任教。'
BOOKS='付妮教授主持编写了《国民基础视唱教程》、《节奏与读谱训练教程》、《精品视唱》、《基础乐理教与学实用教程》，分别在人民音乐出版社、高等教育出版社、上海音乐出版社和中央音乐学院出版社正式出版（专业类教材共计31部册）。'
def claim(raw,category='教学活动',**kw):
 return Claim(id=kw.pop('id','c1'),raw_text=raw,category=category,raw_locator='',date_label='',**kw)
def books():return with_candidate_identity_context(enrich_input([],BIO+BOOKS,'付妮'),'付妮')
def book(title='国民基础视唱教程'):return next(c for c in books() if c.entities.get('title')==title)
def evidence(snippet,**kw):return Evidence(url=kw.pop('url','https://press.example.org/book'),title='出版社书目',publisher='独立出版社',source_tier='C',snippet=snippet,**kw)

class FacultyRecallTests(unittest.TestCase):
 def test_missing_degree_added_without_guessed_year_or_level(self):
  cs=enrich_input([claim(BIO,'任职')],BIO,'付妮');c=next(c for c in cs if c.category=='学历')
  self.assertIn('学校=中央音乐学院',c.elements);self.assertIn('专业=视唱练耳',c.elements)
  self.assertFalse(any('学位' in e or '年份' in e for e in c.elements));self.assertEqual(c.date_label,'')

 def test_explicit_graduation_year_and_stage_survive(self):
  c=enrich_input([],'付妮2018年本科毕业于中央音乐学院视唱练耳专业。','付妮')[0]
  self.assertEqual(c.date_label,'2018');self.assertIn('学段=本科',c.elements)

 def test_same_school_different_stages_not_deduplicated(self):
  cs=enrich_input([],'付妮本科毕业于中央音乐学院，硕士毕业于中央音乐学院。','付妮')
  self.assertEqual(len(cs),2)

 def test_other_person_degree_and_authorship_not_added(self):
  text='付妮的导师王明毕业于中央音乐学院。导师王明编著了《基本乐理教程》。付妮的学生编著了《另外一部教程》。'
  self.assertEqual(enrich_input([],text,'付妮'),[])

 def test_authored_books_atomic_and_unbound_publishers_not_forced(self):
  cs=books();titled=[c for c in cs if c.category=='著作' and c.entities.get('title')]
  self.assertEqual(len(titled),4)
  self.assertTrue(all(not any(e.startswith('出版社=') for e in c.elements) for c in titled))
  totals=[c for c in cs if c.entities.get('publication_total')]
  self.assertEqual(len(totals),1);self.assertEqual(totals[0].elements,['出版数量=31部册'])

 def test_model_merged_list_replaced_by_atoms_and_total(self):
  merged=claim(BOOKS,entities={'title':'国民基础视唱教程'})
  cs=enrich_input([merged],BOOKS,'付妮')
  self.assertEqual(sum(bool(c.entities.get('title')) for c in cs),4)
  self.assertEqual(sum(bool(c.entities.get('publication_total')) for c in cs),1)

 def test_model_existing_atomic_book_is_not_reexpanded_or_given_all_books(self):
  existing=claim(BOOKS,'著作',entities={'title':'国民基础视唱教程'},elements=['书名=国民基础视唱教程','书名=节奏与读谱训练教程','出版数量=31部册'])
  cs=enrich_input([existing],BOOKS,'付妮');c=next(c for c in cs if c.id==existing.id)
  self.assertEqual(c.elements,['书名=国民基础视唱教程','角色=主持编写'])

 def test_reference_books_songs_and_papers_are_not_authored_books(self):
  for raw in ['付妮使用教材《基本乐理》由中央音乐学院出版社出版。','参考文献：《基本乐理》付妮编著。','付妮首演《音乐作品》并出版演出海报。','付妮发表论文《一种算法》于出版社期刊[J]。']:
   self.assertEqual(authored_works(raw,'付妮'),[],raw)

 def test_book_plan_queries_title_before_general_teaching(self):
  p=build_plan(book(),'付妮',ResumeProfile(identity='creator'))
  self.assertEqual(p.queries[0].text,'"付妮" "国民基础视唱教程"')
  self.assertTrue(any('ISBN' in q.text for q in p.queries));self.assertEqual(p.budget['searches'],4)

 def test_vague_graduation_plan_does_not_guess_push_or_doctorate(self):
  c=next(c for c in books() if c.category=='学历');p=build_plan(c,'付妮',ResumeProfile(identity='researcher'))
  self.assertIn('毕业',p.queries[0].text)
  self.assertTrue(any('1980' in q.text for q in p.queries))
  self.assertFalse(any(q.source in {'education_route:推免','education_route:硕博连读'} for q in p.queries))

 def test_supervision_queries_differ_from_course_video(self):
  c=with_candidate_identity_context([*books(),claim('她担任视唱练耳研究生导师')],'付妮')[-1]
  p=build_plan(c,'付妮',ResumeProfile(identity='researcher'))
  self.assertTrue(any('答辩' in q.text and q.site=='ccom.edu.cn' for q in p.queries))
  p=build_plan(claim('独立出版乐理教程、乐理教学视频'),'付妮',ResumeProfile(identity='creator'))
  self.assertTrue(any('教学视频' in q.text for q in p.queries))

 def test_only_name_or_other_book_does_not_match(self):
  c=book()
  self.assertFalse(check_person(c,'https://a.example.org','付妮','国民基础视唱教程').allowed)
  self.assertFalse(check_person(c,'https://a.example.org','作者：付妮 编著\n另一本书\nISBN：123','国民基础视唱教程').allowed)

 def test_book_responsibility_fields_establish_identity_without_affiliation(self):
  text='国民基础视唱教程\n作者/编者：付妮 编著\n出版社：独立出版社\nISBN：9787810960014'
  self.assertTrue(check_person(book(),'https://a.example.org',text).allowed)

 def test_library_book_row_and_final_excerpt_both_match(self):
  title='基础乐理教与学实用教程';c=book(title)
  text='索取号 正题名 责任者\nJ613/Z68(y)\n基础乐理教与学实用教程\n孙从音,付妮主编\nJ614/X99\n另一部书\n王明主编'
  self.assertTrue(check_person(c,'https://library.other.edu.cn/list',text).allowed)
  self.assertTrue(check_person(c,'https://library.other.edu.cn/list',title+'\n孙从音,付妮主编').allowed)

 def test_adjacent_books_cannot_borrow_authors(self):
  text='［书目条目 · 栏1］\n国民基础视唱教程\n作者/编者：王明 主编\n出版社：独立出版社\nISBN：123\n［书目条目 · 栏2］\n另一部教程\n作者/编者：付妮 编著\n出版社：独立出版社\nISBN：456'
  self.assertFalse(check_person(book(),'https://a.example.org',text).allowed)

 def test_another_author_role_and_count_cannot_prove_candidate(self):
  c=book();c.entities['candidate_name']='付妮'
  ev=evidence('国民基础视唱教程，王明主持编写。作者：付妮 编著。',supports=['书名=国民基础视唱教程','角色=主持编写','出版数量=31部册'])
  self.assertEqual(book_supports(c,ev),['书名=国民基础视唱教程'])
  c.elements=['角色=主编'];ev=evidence('执行主编：付妮',supports=['角色=主编'])
  self.assertEqual(book_supports(c,ev),[])

 def test_original_book_count_remains_unknown_with_one_book(self):
  c=next(c for c in books() if c.entities.get('publication_total'));c.entities['candidate_name']='付妮'
  ev=evidence('付妮，中央音乐学院教授，出版国民基础视唱教程。',supports=c.elements)
  result=judge.assess(c,[ev]);self.assertNotIn('出版数量=31部册',result.proved)

 def test_input_and_current_school_exclusion_still_apply_to_books(self):
  c=book();p=policy(['https://music.cuhk.edu.cn/faculty/ni-fu'],['cuhk.edu.cn'],['香港中文大学（深圳）'])
  c.entities['excluded_employer_sources']=for_claim(c,p)
  self.assertTrue(excluded('https://diligentia.cuhk.edu.cn/book',claim_sources(c)))
  e=evidence('国民基础视唱教程\n作者：付妮 编著\nISBN：123',url='https://music.cuhk.edu.cn/book',supports=c.elements)
  self.assertEqual(judge.assess(c,[e]).evidence,[])

 def test_book_extract_cannot_invent_an_excerpt_or_role(self):
  c=book();text='国民基础视唱教程\n作者：付妮 编著\n出版社：独立出版社\nISBN：123'
  class LLM:
   def complete_json(self,*a,**k):return {'snippet':text,'supports':c.elements,'publisher':'独立出版社'}
  ev=extract_evidence(c,SearchHit('https://a.example.org/book',title='国民基础视唱教程'),text,LLM(),candidate_name='付妮')
  self.assertIsNotNone(ev);self.assertEqual(ev.supports,['书名=国民基础视唱教程'])
  class Invent:
   def complete_json(self,*a,**k):return {'snippet':text+'付妮主持编写','supports':c.elements}
  self.assertIsNone(extract_evidence(c,SearchHit('https://a.example.org/book',title='国民基础视唱教程'),text,Invent(),candidate_name='付妮'))

 def test_two_column_pdf_keeps_responsibility_with_correct_book(self):
  import pymupdf
  doc=pymupdf.open();p=doc.new_page(width=800,height=500)
  for x,title,author in [(20,'First Aural Text','Ni Fu'),(420,'Second Aural Text','Wang Ming')]:
   for y,line in [(30,title),(70,'作者/编者: '+author+' 编著'),(100,'出版社: 合成出版社'),(130,'ISBN: 9787810960014')]:
    p.insert_text((x,y),line,fontname='china-s',fontsize=11)
  text=pdf_catalog_text(doc.tobytes());doc.close()
  c=claim('Ni Fu 编著《Second Aural Text》','著作',entities={'title':'Second Aural Text'})
  c=with_candidate_identity_context([c],'Ni Fu')[0]
  self.assertFalse(check_person(c,'https://press.example.org/catalog',text).allowed)
  c=c.model_copy(update={'raw_text':'Ni Fu 编著《First Aural Text》','entities':{**c.entities,'title':'First Aural Text'}})
  self.assertTrue(check_person(c,'https://press.example.org/catalog',text).allowed)

 def test_teacher_employment_and_supervised_degree_cannot_prove_own_education(self):
  from app.identity import own_education_record
  self.assertFalse(own_education_record('付妮，中央音乐学院作曲系副教授，硕士生导师。','人物介绍','付妮'))
  self.assertFalse(own_education_record('付妮教授讲授本科和硕士课程。','导师介绍','付妮'))
  self.assertTrue(own_education_record('付妮，1991年毕业于中央音乐学院视唱练耳专业。','','付妮'))
  self.assertTrue(own_education_record('张三通过推荐免试进入哈尔滨工业大学攻读硕士。','','张三'))

 def test_role_metadata_does_not_falsely_contradict_hosting_writing(self):
  from app.faculty_recall import book_contradicts
  c=book();e=evidence('国民基础视唱教程\n作者：付妮 编著\nISBN：123',contradicts=['角色=主持编写'])
  self.assertEqual(book_contradicts(c,e),[])

 def test_primary_book_catalog_is_read_before_encyclopedia(self):
  from app.collect import _relevance
  c=book('基础乐理教与学实用教程')
  official=SearchHit('https://tsg.xacom.edu.cn/catalog.pdf',title='馆藏书目',snippet='基础乐理教与学实用教程 孙从音,付妮主编')
  encyclopedia=SearchHit('https://baike.baidu.com/x',title='基础乐理教与学实用教程',snippet='付妮 编著')
  self.assertGreater(_relevance(official,'付妮',c),_relevance(encyclopedia,'付妮',c))

 def test_alternative_index_still_enforces_exact_target_domain(self):
  from app.search import ZhipuSearcher,CachedSearcher
  from app.source_guard import SourceGuardSearcher
  s=ZhipuSearcher(api_key='fixture',engine='search_std');s.engine_site_fallback='';s.engine_open='search_pro_sogou'
  allowed=SearchHit('https://grad.ccom.edu.cn/schedule',snippet='付妮 导师')
  own=SearchHit('https://music.cuhk.edu.cn/faculty/ni-fu')
  spoof=SearchHit('https://ccom.edu.cn.example.net/record')
  g=SourceGuardSearcher(CachedSearcher(s),policy(domains=['cuhk.edu.cn']))
  with patch.object(s,'_call',side_effect=[[],[],[allowed,own,spoof]]) as call:
   self.assertEqual(g.search_with_confirmation('付妮 答辩',site='ccom.edu.cn'),[allowed])
   self.assertEqual(g.search_with_confirmation('付妮 答辩',site='ccom.edu.cn'),[allowed])
   self.assertEqual(call.call_count,3)
   self.assertIn('-site:cuhk.edu.cn',call.call_args.args[0])
   self.assertEqual(call.call_args.args[2],s.engine_open_alternate)

 def test_publisher_pdf_query_exists_without_guessing_author_role(self):
  p=build_plan(book(),'付妮',ResumeProfile(identity='creator'))
  self.assertTrue(any('书目 filetype:pdf' in q.text for q in p.queries))
  self.assertFalse(any('执行主编' in q.text for q in p.queries[:4]))

 def test_catalog_query_switches_index_without_changing_normal_search(self):
  from app.search import ZhipuSearcher
  s=ZhipuSearcher(api_key='fixture');s.engine_open='search_pro_sogou';s.engine_open_alternate='search_pro_quark'
  with patch.object(s,'_call',return_value=[]) as call:
   s.search('付妮 教材');call.assert_called_with('付妮 教材',None,'search_pro_sogou')
   s.search('付妮 基础乐理 书目 filetype:pdf');call.assert_called_with('付妮 基础乐理 书目 filetype:pdf',None,'search_pro_quark')

 def test_caller_cached_searcher_keeps_confirmation_and_report_cache_isolation(self):
  from app.search import CachedSearcher
  class Provider:
   calls=0
   def search(self,*a,**k):return []
   def confirm_empty(self,*a,**k):self.calls+=1;return [SearchHit('https://ccom.edu.cn/schedule')]
  provider=Provider();caller=CachedSearcher(provider);report=CachedSearcher(caller)
  self.assertTrue(report.search_with_confirmation('q',site='ccom.edu.cn'));self.assertEqual(provider.calls,1)
  self.assertEqual(caller.cache,{})

 def test_loading_only_library_uses_marked_excerpt_without_fabricating_role(self):
  from app.collect import _read_and_extract,Budget
  c=book('基础乐理教与学实用教程');c.entities['candidate_name']='付妮'
  snippet='基础乐理教与学实用教程\n主编孙从音, 付妮\n中央音乐学院出版社 2010'
  hit=SearchHit('https://library.other.edu.cn/catalog',title='图书馆书目检索',snippet=snippet)
  class Fetcher:
   failures={}
   def get(self,url):return '<html><body>统一检索 Loading...</body></html>'
  class LLM:
   def complete_json(self,*a,**k):return {'snippet':snippet,'supports':['书名=基础乐理教与学实用教程','角色=主持编写']}
  result=_read_and_extract(c,[hit],Fetcher(),LLM(),set(),candidate_name='付妮',budget=Budget())
  self.assertEqual(len(result),1);self.assertEqual(result[0].extraction_method,'search_snippet')
  self.assertEqual(result[0].supports,['书名=基础乐理教与学实用教程'])

 def test_opac_rows_bind_author_after_copies_but_not_another_book(self):
  c=book('基础乐理教与学实用教程')
  good='图书馆\n中文图书 1.基础乐理教与学实用教程 J613/S766\n馆藏复本：1\n可借复本：1 主编孙从音, 付妮\n中央音乐学院出版社 2010'
  self.assertTrue(check_person(c,'https://library.other.edu.cn',good).allowed)
  wrong=good.replace('可借复本：1 主编孙从音, 付妮','另一部教材\n付妮主编')
  self.assertFalse(check_person(c,'https://library.other.edu.cn',wrong).allowed)

 def test_supervision_query_uses_discipline_without_field_label(self):
  c=claim('她担任研究生导师',entities={'candidate_identity_context':{'education_fields':[{'field':'视唱练耳专业'}],'education_schools':['中央音乐学院']}})
  from app.faculty_recall import queries_for
  qs=queries_for(c,'付妮',ResumeProfile(identity='researcher'))
  self.assertTrue(any(q.text=='"付妮" 视唱练耳 答辩' and q.site=='ccom.edu.cn' for q in qs))

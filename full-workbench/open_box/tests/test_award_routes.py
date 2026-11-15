"""高教社杯/EAEDU 漏检、错赛段与受限搜索补查回归。"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app.collect import _search_resilient, extract_evidence
from app.competitions import (claim_province, competition_for_claim, competition_resolution_note,
                              competition_page_mismatch, find_competition, roster_links, seed_pages, useful_roster_hit)
from app.schema import Claim
from app.search import (CachedSearcher, FallbackSearcher, PageFetcher, SearchHit,
                        SearchUnavailable, ZhipuSearcher, _pdf_text)
from app.strategy import build_plan


def award(contest="全国高等院校英语能力大赛", year="2024", **entities):
    return Claim(id="award-route", raw_text=f"{year} {contest} 正式省赛一等奖", raw_locator="简历",
                 category="竞赛", date_label=year, elements=[f"赛事={contest}","奖项=一等奖"],
                 entities={"contest":contest, **entities})


class AwardRouteTests(unittest.TestCase):
    def test_english_ability_event_is_separate_from_neccs(self):
        for label in ("全国高等院校英语能力大赛", "高等院校大学生英语能力大赛",
                      "全国高等院校（大学生）英语能力大赛"):
            self.assertEqual(find_competition(label)["id"],"eaedu")
        self.assertEqual(find_competition("全国大学生英语竞赛")["id"],"neccs")

    def test_sponsor_brand_needs_subject_context(self):
        self.assertIsNone(find_competition("2025 高教社杯二等奖"))
        self.assertEqual(find_competition("高教社杯数学建模省二等奖")["id"],"cumcm")
        self.assertEqual(find_competition("高教社杯先进成图大赛")["id"],"engineering-drawing")
        c=award("高教社杯");c.raw_text="2024高教社杯数学建模省一等奖"
        self.assertEqual(competition_for_claim(c)["id"],"cumcm")
        c.raw_text="2024高教社杯省一等奖"
        self.assertIn("冠名",competition_resolution_note(c))

    def test_english_plan_uses_its_own_site_and_relaxed_title_words(self):
        plan=build_plan(award(school="北京大学"),"张三")
        primary=next(q for q in plan.queries if q.site=="eaedu.org.cn")
        self.assertEqual(primary.text,"高等院校 英语能力大赛 2024 获奖名单")
        self.assertNotIn("chinaneccs.cn",[q.site for q in plan.queries])
        self.assertTrue(primary.confirm_empty)
        self.assertLessEqual(sum(q.confirm_empty for q in plan.queries),2)

    def test_seed_does_not_use_current_finals_for_old_year(self):
        item=find_competition("全国高等院校英语能力大赛")
        self.assertFalse(any("finalrank" in p["url"] for p in seed_pages(item,award())))
        c=award(year="2026");c.raw_text="2026 全国高等院校英语能力大赛全国决赛一等奖"
        self.assertTrue(any("finalrank" in p["url"] for p in seed_pages(item,c)))

    def test_math_formal_notice_replaces_dead_initial_attachment(self):
        c=award("高教社杯数学建模竞赛","2025");c.raw_text="2025 高教社杯数学建模全国一等奖"
        pages=seed_pages(competition_for_claim(c),c)
        self.assertTrue(any("f1241bf39c38153b57bdb27125fa2d72" in p["url"] for p in pages))
        self.assertFalse(any(p.get("inactive") for p in pages))
        c.raw_text="2025 高教社杯数学建模省二等奖"
        self.assertFalse(any(p.get("level")=="national" for p in seed_pages(competition_for_claim(c),c)))

    def test_province_does_not_come_from_school_name(self):
        self.assertEqual(claim_province(award(school="湖南大学")),"")
        c=award(province="广东");self.assertEqual(claim_province(c),"广东")
        c=award();c.raw_text+=" 广东赛区";self.assertEqual(claim_province(c),"广东")

    def test_roster_follows_year_province_and_formal_student_track(self):
        c=award(province="广东")
        html="""<html><a href='/news-109.html'>2024云南赛区省赛获奖名单</a>
        <a href='/news-110.html'>2024广东赛区省赛获奖名单</a>
        <a href='/news-111.html'>2024高等院校大学生英语能力大赛青年教师赛道获奖名单</a>
        <a href='/news-112.html'>2024模拟赛获奖名单</a>
        <a href='/news-113.html'>2025广东赛区省赛获奖名单</a></html>"""
        page=SearchHit("https://www.eaedu.org.cn/rank-1-2-1.html",title="获奖名单")
        self.assertEqual([p['url'] for p in roster_links(html,page,c)],
                         ["https://www.eaedu.org.cn/news-110.html"])
        c.raw_text="2024全国高等院校英语能力大赛模拟赛一等奖"
        self.assertTrue(any("news-112" in p['url'] for p in roster_links(html,page,c)))

    def test_wrong_stage_is_rejected_before_model_cost(self):
        class NoModel:
            def complete_json(self,*args,**kwargs):
                raise AssertionError("不同赛段不应花模型费用")
        for title in ("2024模拟赛获奖名单","2024大学生英语能力大赛青年教师赛道获奖名单",
                      "2024大学生英语校内选拔赛获奖名单"):
            self.assertIsNone(extract_evidence(award(),SearchHit("https://www.eaedu.org.cn/news-1.html",title=title),
                                              "张三 一等奖",NoModel(),candidate_name="张三"))

    def test_national_claim_does_not_use_province_sample_or_hide_mixed_student_roster(self):
        c=award(province="广东");c.raw_text="2024高等院校英语能力大赛全国决赛一等奖"
        self.assertFalse(any(p.get("level")=="provincial" for p in seed_pages(competition_for_claim(c),c)))
        self.assertTrue(competition_page_mismatch(c,"2024广东省赛获奖名单"))
        self.assertEqual(competition_page_mismatch(c,"2024学生获奖及优秀指导教师名单"),"")

    def test_entry_year_outweighs_current_portal_title(self):
        c=award();snippet="2025 张三 一等奖"
        class Model:
            def complete_json(self,*a,**k):return dict(snippet=snippet,supports=c.elements)
        ev=extract_evidence(c,SearchHit("https://eaedu.org.cn/list",title="2024往届获奖查询"),snippet,Model(),candidate_name="张三")
        self.assertFalse(ev.supports)

    def test_noisy_std_result_can_trigger_confirmation(self):
        c=award("高教社杯数学建模竞赛","2025")
        noise=SearchHit("https://www.mcm.edu.cn/teacher.pdf",title="全国数学建模微课程教学竞赛名单")
        good=SearchHit("https://www.mcm.edu.cn/2025.html",title="2025全国大学生数学建模竞赛获奖名单")
        class Provider:
            calls=0
            def search(self,*args,**kwargs):return [noise]
            def confirm_empty(self,*args,**kwargs):self.calls+=1;return [good]
        provider=Provider();cached=CachedSearcher(provider)
        hits=_search_resilient("数学建模 2025 获奖名单","mcm.edu.cn",cached,"",lambda _:None,True,c)
        self.assertIn(good,hits);self.assertFalse(useful_roster_hit(c,noise))
        _search_resilient("数学建模 2025 获奖名单","mcm.edu.cn",cached,"",lambda _:None,True,c)
        self.assertEqual(provider.calls,1)


class SearchConfirmationTests(unittest.TestCase):
    def test_concurrent_negative_cache_pays_only_one_confirmation(self):
        class Provider:
            searches=0;confirmations=0
            def search(self,*args,**kwargs):self.searches+=1;return []
            def confirm_empty(self,*args,**kwargs):
                self.confirmations+=1;return [SearchHit("https://www.eaedu.org.cn/news-110.html")]
        provider=Provider();cached=CachedSearcher(provider)
        barrier=threading.Barrier(4)
        def run(_):
            barrier.wait();return cached.search_with_confirmation("2024获奖名单",site="eaedu.org.cn")
        with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(run,range(4)))
        self.assertTrue(all(result));self.assertEqual((provider.searches,provider.confirmations),(1,1))

    def test_positive_result_and_open_query_do_not_upgrade(self):
        class Provider:
            confirmations=0
            def search(self,q,site=None):return [SearchHit("https://eaedu.org.cn/news.html")] if site else []
            def confirm_empty(self,*a,**k):self.confirmations+=1;return []
        provider=Provider();cached=CachedSearcher(provider)
        cached.search_with_confirmation("q","eaedu.org.cn");cached.search_with_confirmation("open")
        self.assertEqual(provider.confirmations,0)

    def test_non_confirming_provider_and_filtered_failures_remain_compatible(self):
        class Provider:
            def search(self,*a,**k):return []
        self.assertEqual(CachedSearcher(Provider()).search_with_confirmation("q","eaedu.org.cn"),[])
        with patch.object(Provider,"search",side_effect=SearchUnavailable("余额不足")):
            with self.assertRaises(SearchUnavailable):
                CachedSearcher(Provider()).search_with_confirmation("q","eaedu.org.cn")

    def test_zhipu_confirmation_preserves_shared_engine_and_respects_existing_fallback(self):
        s=ZhipuSearcher(api_key="test",engine="search_std");s.engine_site_fallback=""
        with patch.object(s,"_call",return_value=[]) as call:
            s.confirm_empty("q","eaedu.org.cn")
            call.assert_called_once_with("q","eaedu.org.cn","search_pro")
        self.assertEqual(s.engine,"search_std")
        for site,fallback,engine in ((None,"","search_std"),("eaedu.org.cn","search_pro","search_std"),
                                     ("eaedu.org.cn","","search_pro")):
            s.engine_site_fallback=fallback;s.engine=engine
            with patch.object(s,"_call") as call:
                self.assertEqual(s.confirm_empty("q",site),[]);call.assert_not_called()

    def test_confirmation_survives_provider_wrapper(self):
        class Provider:
            def search(self,*a,**k):return []
            def confirm_empty(self,*a,**k):return [SearchHit("https://eaedu.org.cn/result")]
        s=CachedSearcher(FallbackSearcher(Provider(),Provider()))
        self.assertTrue(s.search_with_confirmation("q","eaedu.org.cn"))

    def test_scanned_pdf_page_breaks_are_not_readable_text(self):
        # PyMuPDF 是可选依赖；没装的环境也要能跑这条测试，所以用桩模块代替真的 pymupdf。
        import sys
        from types import SimpleNamespace as _NS
        fake_pymupdf=_NS(open=lambda *a,**k:(_ for _ in ()).throw(ValueError("scan")))
        with patch.dict(sys.modules,{"pymupdf":fake_pymupdf}), \
                patch("pdfminer.high_level.extract_text",return_value="\f\f\n "):
            self.assertIsNone(_pdf_text(b"%PDF-1.4\nscan"))

    def test_public_19mb_pdf_is_passed_to_parser(self):
        blob=b"%PDF-1.4\n"+b"0"*(19*1024*1024)
        response=SimpleNamespace(content=blob,status_code=200,url="https://mcm.edu.cn/list.pdf",
                                 headers={},raise_for_status=lambda:None)
        with patch("app.search.robots_allows",return_value=True), \
                patch("httpx.get",return_value=response),patch("app.search._pdf_text",return_value="张三 一等奖") as parse:
            self.assertEqual(PageFetcher().get(response.url),"张三 一等奖")
            parse.assert_called_once()

    def test_expired_ssl_is_not_retried_for_each_award_page(self):
        import httpx
        f=PageFetcher()
        with patch("app.search.robots_allows",return_value=True), \
                patch("httpx.get",side_effect=httpx.ConnectError("CERTIFICATE_VERIFY_FAILED: certificate has expired")) as request:
            self.assertIsNone(f.get("https://eaedu.org.cn/list"))
            self.assertIsNone(f.get("https://eaedu.org.cn/result"))
        self.assertEqual(request.call_count,1)
        self.assertIn("证书已过期",f.failures["https://eaedu.org.cn/result"])


if __name__=="__main__":unittest.main()

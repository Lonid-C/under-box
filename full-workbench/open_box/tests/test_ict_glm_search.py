"""ICT-only provider selection; synthetic credentials and no network."""
import os,unittest
from unittest.mock import patch
from app.search import build_searcher,ZhipuSearcher,NullSearcher
from app.api_config import SearchCredentials,bind_search_credentials
from app.retrieval_policy import RecallFallbackSearcher

SETTINGS={'UNDERBOX_ICT_GLM_ONLY':'1','UNDERBOX_ICT_RECALL':'1','SEARCH_PROVIDER':'deepseek',
          'SEARCH_API_KEY':'synthetic-glm','DEEPSEEK_API_KEY':'synthetic-ds','BRAVE_SEARCH_API_KEY':'synthetic-brave',
          'SEARCH_ENGINE':'search_std','SEARCH_ENGINE_OPEN':'search_pro_sogou'}

class GLMSearchTests(unittest.TestCase):
    def test_ict_policy_forces_official_glm_route(self):
        with patch.dict(os.environ,{**SETTINGS,'SEARCH_ENDPOINT':'https://custom.invalid/search'}):
            search=build_searcher('deepseek')
        self.assertIs(type(search),ZhipuSearcher)
        self.assertEqual(search.endpoint,ZhipuSearcher.DEFAULT_ENDPOINT)
        self.assertEqual(search.engine,'search_std');self.assertEqual(search.engine_open,'search_pro_sogou')

    def test_empty_glm_result_never_constructs_deepseek_backup(self):
        with patch.dict(os.environ,SETTINGS),patch('app.deepseek_search.DeepSeekSearcher') as backup,patch.object(ZhipuSearcher,'_call',return_value=[]):
            self.assertEqual(build_searcher().search('合成查询',site='example.com'),[])
        backup.assert_not_called()

    def test_missing_glm_key_does_not_borrow_deepseek_or_brave(self):
        with patch.dict(os.environ,{**SETTINGS,'SEARCH_API_KEY':''}):search=build_searcher()
        self.assertIs(type(search),NullSearcher)

    def test_account_key_remains_exclusive(self):
        with patch.dict(os.environ,SETTINGS),bind_search_credentials(SearchCredentials(api_key='synthetic-account')):
            search=build_searcher()
        self.assertIs(type(search),ZhipuSearcher);self.assertEqual(search.api_key,'synthetic-account')

    def test_absent_policy_keeps_existing_configured_route(self):
        with patch.dict(os.environ,{**SETTINGS,'UNDERBOX_ICT_GLM_ONLY':'0'}):search=build_searcher('zhipu')
        self.assertIsInstance(search,RecallFallbackSearcher)

if __name__=='__main__':unittest.main()

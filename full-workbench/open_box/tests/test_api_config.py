import os
import unittest
from unittest.mock import patch
from app.api_config import SearchCredentials, bind_search_credentials, search_credentials
from app.search import build_searcher, ZhipuSearcher, FallbackSearcher
from app.llm import build_llm
from app.usage import UsageExecutor


class ApiConfigTests(unittest.TestCase):
    def test_bound_search_key_is_exclusive_and_deepseek_stays_shared(self):
        with patch.dict(os.environ, {'SEARCH_PROVIDER':'zhipu','SEARCH_API_KEY':'synthetic-shared',
                                     'SEARCH_ENDPOINT':'https://custom.invalid/search',
                                     'BRAVE_SEARCH_API_KEY':'synthetic-brave',
                                     'LLM_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'synthetic-ds'}):
            original=dict(os.environ)
            with bind_search_credentials(SearchCredentials('synthetic-demo')):
                searcher=build_searcher()
                self.assertIsInstance(searcher,ZhipuSearcher)
                self.assertEqual(searcher.api_key,'synthetic-demo')
                self.assertEqual(searcher.endpoint,ZhipuSearcher.DEFAULT_ENDPOINT)
                self.assertEqual(build_llm().api_key,'synthetic-ds')
                self.assertEqual(build_searcher('brave').api_key,'synthetic-demo')
            self.assertIsInstance(build_searcher(),FallbackSearcher)
            self.assertEqual(build_searcher().primary.api_key,'synthetic-shared')
            self.assertEqual(dict(os.environ),original)

    def test_nested_pools_preserve_credential_and_reset_on_exception(self):
        def nested():
            with UsageExecutor(max_workers=2) as pool:
                return list(pool.map(lambda _:build_searcher().api_key,range(3)))
        try:
            with bind_search_credentials(SearchCredentials('synthetic-a')):
                with UsageExecutor(max_workers=2) as pool:
                    self.assertEqual(list(pool.map(lambda _:nested(),range(2))),[['synthetic-a']*3]*2)
                with bind_search_credentials(SearchCredentials('synthetic-b')):
                    self.assertEqual(build_searcher().api_key,'synthetic-b')
                self.assertEqual(build_searcher().api_key,'synthetic-a')
                raise RuntimeError('synthetic failure')
        except RuntimeError:
            pass
        self.assertIsNone(search_credentials())

    def test_credentials_validation_and_repr_do_not_reveal_key(self):
        self.assertNotIn('synthetic-secret',repr(SearchCredentials('synthetic-secret')))
        for value in ('','bad key','bad\nkey','中文'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):SearchCredentials(value)

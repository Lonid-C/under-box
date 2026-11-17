"""Failure summaries remain accurate after the display event cap; no network."""
import json,unittest
from types import SimpleNamespace
from app.collect import Budget
from app.data_sources import AgentRun

class FailureRecordTests(unittest.TestCase):
    def run_record(self):
        return AgentRun(SimpleNamespace(id='synthetic'),SimpleNamespace(id='c1'),SimpleNamespace(queries=[],notes=[]),Budget())
    def test_failure_counts_and_safe_examples_survive_event_cap(self):
        run=self.run_record()
        for i in range(120):run.add({'action':'search','state':'returned'})
        for i in range(151):run.add({'action':'read','state':'failed','error':'ReadUnavailable','reason':'HTTP 403','reference':'https://user:password@source.example/'+str(i)+'?api_key=synthetic-secret'})
        snapshot=run.snapshot();self.assertEqual(len(snapshot['events']),80);self.assertTrue(snapshot['truncated'])
        self.assertEqual(snapshot['failureGroups'][0]['count'],151);self.assertEqual(len(snapshot['failureGroups'][0]['references']),3)
        self.assertEqual(snapshot['counts']['read']['failed'],151)
        self.assertNotIn('synthetic-secret',json.dumps(snapshot));self.assertNotIn('user:password@',json.dumps(snapshot))
    def test_query_errors_and_empty_read_are_not_empty_search_results(self):
        run=self.run_record()
        for event in [{'action':'search','state':'empty'},{'action':'read','state':'empty'},{'action':'search','state':'failed','error':'SearchUnavailable'}]:run.add(event)
        groups=run.snapshot()['failureGroups'];self.assertEqual(len(groups),2)
        self.assertEqual(groups[0]['error'],'EmptyRead');self.assertEqual(groups[1]['action'],'search')
    def test_failure_group_cap_is_explicit_and_totals_are_not_truncated(self):
        run=self.run_record()
        for i in range(60):run.add({'action':'read','state':'failed','error':'ReadUnavailable','reason':'synthetic-'+str(i)})
        snapshot=run.snapshot();self.assertTrue(snapshot['failureGroupsTruncated']);self.assertEqual(len(snapshot['failureGroups']),48)
        self.assertEqual(snapshot['counts']['read']['failed'],60)
    def test_snapshot_groups_are_detached_from_live_counters(self):
        run=self.run_record();event={'action':'read','state':'failed','error':'ReadUnavailable','reason':'HTTP 404','reference':'https://source.example/one'}
        run.add(event);first=run.snapshot();run.add({**event,'reference':'https://source.example/two'})
        self.assertEqual(first['failureGroups'][0]['count'],1);self.assertEqual(first['failureGroups'][0]['references'],['https://source.example/one'])

"""Offline-only tests for durable Autopilot diagnostic job queue."""
import sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import diagnostic_jobs as jobs
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.path=Path(self.tmp.name)
  p=patch.object(jobs,'DB',self.path/'jobs.sqlite3');p.start();self.addCleanup(p.stop)
  p=patch.object(jobs,'LOCK',self.path/'jobs.lock');p.start();self.addCleanup(p.stop)
 def fake_health(self):
  return {'severity':'OK','errors':[],'warnings':[],
          'database':{'pending_count':0,'positions':2,'closed_trades':4,'restricted_pairs':['OP/USDT']}}
 def test_health_records_no_mutations(self):
  with patch.object(jobs.monitor,'snapshot',side_effect=self.fake_health):
   task=jobs.add('health')
   result=jobs.one()
  self.assertEqual(result['status'],'done')
  self.assertEqual(result['job_id'],task)
  self.assertEqual(jobs.summary()['counts'],{'done':1})
  self.assertFalse(jobs.summary()['auto_trading'])
  self.assertEqual(jobs.one(),None)
 def test_invalid_job_rejected(self):
  with self.assertRaises(ValueError):jobs.add('exec arbitrary shell')
 def test_triage_needs_review_on_critical(self):
  def critical():
   r=self.fake_health();r['severity']='CRITICAL'
   r['database']['pending_count']=1
   r['errors']=['V2_UNRESOLVED_ORDERS_OLDER_10_MIN']
   return r
  with patch.object(jobs.monitor,'snapshot',side_effect=critical):
   jobs.add('triage');result=jobs.one()
  self.assertEqual(result['status'],'needs_review')
  self.assertEqual(jobs.summary()['recent'][0]['result']['recommendation'],
                   'READ_ONLY_RECONCILIATION_NO_RETRY')
 def test_error_not_retried(self):
  with patch.object(jobs.monitor,'snapshot',side_effect=RuntimeError('offline')):
   jobs.add('health');a=jobs.one()
  self.assertEqual(a['status'],'uncertain')
  self.assertIsNone(jobs.one())
 def test_campaign_pnl_only_reads_test_db(self):
  path=self.path/'campaign.db'
  with sqlite3.connect(path) as c:
   c.execute('CREATE TABLE closed_trades(pnl REAL)')
   c.executemany('INSERT INTO closed_trades VALUES (?)',[(1.25,),(-1.50,)])
  with patch.object(jobs.monitor,'snapshot',side_effect=self.fake_health),patch.object(jobs.monitor,'DB',path):
   jobs.add('campaign');r=jobs.one()
  self.assertEqual(r['status'],'done')
  result=jobs.summary()['recent'][0]['result']
  self.assertEqual(result['closed'],2)
  self.assertEqual(result['wins'],1)
  self.assertAlmostEqual(result['realized_pnl_usdt'],-.25)
if __name__=='__main__':unittest.main(verbosity=2)

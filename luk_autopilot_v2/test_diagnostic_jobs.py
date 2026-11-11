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
 def test_isolation_job_preserves_no_auto_codex(self):
  fake=self.fake_health()
  fake['database'].update({'integrity':'ok','quarantined_count':3})
  fake['services']={n:{'active':True} for n in ('trade','bridge','telegram_menu','notifier')}
  verified={'result':'SANDBOX_ISOLATION_VERIFIED','host_home':'not_mounted',
            'network':'namespace_disabled'}
  with patch.object(jobs.monitor,'snapshot',return_value=fake),patch(
      'isolated_runner.snapshot_proof',return_value=verified) as probe:
   jobs.add('isolation')
   done=jobs.one()
  self.assertEqual(done['status'],'done')
  self.assertEqual(jobs.summary()['recent'][0]['result']['isolation']['result'],
                   'SANDBOX_ISOLATION_VERIFIED')
  self.assertFalse(jobs.summary()['auto_codex'])
  probe.assert_called_once()
 def test_isolation_failure_is_uncertain_without_retries(self):
  with patch.object(jobs.monitor,'snapshot',side_effect=self.fake_health),patch(
      'isolated_runner.snapshot_proof',side_effect=RuntimeError('namespace_unavailable')):
   jobs.add('isolation')
   r=jobs.one()
  self.assertEqual(r['status'],'uncertain')
  self.assertIsNone(jobs.one())
  self.assertEqual(jobs.summary()['counts']['uncertain'],1)
 def test_invalid_job_rejected(self):
  with self.assertRaises(ValueError):jobs.add('exec arbitrary shell')
 def test_warning_pending_needs_review(self):
  def warn():
   r=self.fake_health();r['severity']='WARNING';r['database']['pending_count']=1
   return r
  with patch.object(jobs.monitor,'snapshot',side_effect=warn):
   jobs.add('health');r=jobs.one()
  self.assertEqual(r['status'],'needs_review')
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
 def test_atomic_queue_cap(self):
  with patch.object(jobs.monitor,'snapshot',side_effect=self.fake_health):
   for _ in range(12):jobs.add('health')
   with self.assertRaisesRegex(RuntimeError,'QUEUE_FULL'):
    jobs.add('triage')
   self.assertEqual(jobs.one()['status'],'done')
   self.assertEqual(len(jobs.add('campaign')),32)
 def test_dedup_scheduled(self):
  self.assertTrue(jobs.scheduled('health',3600))
  self.assertFalse(jobs.scheduled('health',3600))
  self.assertEqual(jobs.summary()['counts']['queued'],1)
 def test_symlink_rejected(self):
  original=self.path/'original.db'
  original.write_bytes(b'UNSAFE')
  jobs.DB.symlink_to(original)
  with self.assertRaisesRegex(RuntimeError,'UNSAFE_QUEUE_PATH'):
   jobs.add('health')
  self.assertEqual(original.read_bytes(),b'UNSAFE')
 def test_daemon_flock_rejects_manual_worker(self):
  import fcntl
  # Use child process (flock is per open file description, not thread).
  jobs.add('health')
  with jobs.LOCK.open('a+') as f:
   fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaisesRegex(RuntimeError,'DAEMON_OWNS_QUEUE'):
    jobs.guarded_one()
  self.assertEqual(jobs.summary()['counts']['queued'],1)
if __name__=='__main__':unittest.main(verbosity=2)

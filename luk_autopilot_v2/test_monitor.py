"""Autopilot V2 monitor offline tests: no daemon, Telegram or trading."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT=Path(__file__).with_name('monitor.py')
spec=importlib.util.spec_from_file_location('luk_ap_monitor',SCRIPT)
ap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

class MonitorTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory()
  self.addCleanup(tmp.cleanup)
  self.root=Path(tmp.name)
  self.db=self.root/'test.sqlite3'
  self.log=self.root/'demo.log'
  p=patch.multiple(ap,ROOT=self.root,STATE=self.root/'state.json',
                   LOCK=self.root/'test.lock',DB=self.db,LOG=self.log)
  p.start();self.addCleanup(p.stop)
  with sqlite3.connect(self.db) as c:
   c.execute('CREATE TABLE orders(status TEXT,created_at TEXT)')
   c.execute('CREATE TABLE positions(symbol TEXT)')
   c.execute('CREATE TABLE closed_trades(id INTEGER)')
   c.execute("INSERT INTO orders VALUES ('QUARANTINED','2026-01-01T00:00:00+00:00')")
 def test_readonly_db(self):
  before=self.db.read_bytes()
  result=ap.inspect_db()
  self.assertEqual(result['quarantined_count'],1)
  self.assertEqual(result['pending_count'],0)
  self.assertEqual(result['integrity'],'ok')
  self.assertEqual(self.db.read_bytes(),before)
 def test_pending_age_and_severity(self):
  with sqlite3.connect(self.db) as c:
   c.execute("INSERT INTO orders VALUES ('UNKNOWN','2026-01-01T00:00:00+00:00')")
  active={'active':True,'pid':42,'status':'running'}
  with patch.object(ap,'service_state',return_value=active),patch.object(
      ap,'inspect_logs',return_value={'cycle_age_seconds':20,'recent_errors':0}):
   r=ap.snapshot()
  self.assertEqual(r['severity'],'CRITICAL')
  self.assertIn('V2_UNRESOLVED_ORDERS_OLDER_10_MIN',r['errors'])
  self.assertFalse(r['mutations_authorized'])
 def test_inactive_trader_is_critical(self):
  def state(name):
   return {'active':name!=ap.SERVICES['trade'],'pid':0,'status':'dead'}
  with patch.object(ap,'service_state',side_effect=state),patch.object(
      ap,'inspect_logs',return_value={'cycle_age_seconds':None,'recent_errors':0}):
   r=ap.snapshot()
  self.assertIn('V2_SERVICE_DOWN',r['errors'])
  self.assertEqual(r['severity'],'CRITICAL')
 def test_one_check_never_sends_without_telegram(self):
  with patch.object(ap,'service_state',return_value={'active':True,'pid':3,'status':'running'}),patch.object(
      ap,'inspect_logs',return_value={'cycle_age_seconds':12,'recent_errors':0}),patch.object(
      ap,'notify',side_effect=AssertionError('must not notify')):
   r=ap.run_once(no_telegram=True)
  self.assertEqual(r['mode'],'MONITOR_ONLY')
  self.assertTrue(ap.STATE.exists())
  self.assertEqual(ap.STATE.stat().st_mode & 0o777,0o600)
 def test_alert_dedup_when_unchanged(self):
  report={'severity':'OK','errors':[],'warnings':[]}
  k,t,s=ap.notify(report,{'notification_key':'OK|','notification_epoch':123})
  self.assertEqual((k,t,s),('OK|',123,False))
if __name__=='__main__':unittest.main(verbosity=2)


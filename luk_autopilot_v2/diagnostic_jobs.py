#!/usr/bin/env python3
"""LUK Autopilot V2 queue: durable READ-ONLY diagnostics; no trading/actions."""
import argparse,fcntl,json,os,sqlite3,time,uuid
from datetime import datetime,timezone
from pathlib import Path
import monitor
BASE=Path(__file__).resolve().parent
DB=BASE/'diagnostics.sqlite3'
LOCK=BASE/'diagnostics.lock'
TYPES=('health','campaign','triage')
def now():return datetime.now(timezone.utc).isoformat()
def conn():
 if DB.is_symlink():raise RuntimeError('UNSAFE_QUEUE_PATH')
 c=sqlite3.connect(str(DB),timeout=10)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=10000')
 c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,created TEXT,finished TEXT,result TEXT)')
 os.chmod(DB,0o600)
 return c
def add(kind):
 if kind not in TYPES:raise ValueError('UNSUPPORTED_DIAGNOSTIC')
 with conn() as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=12:
   raise RuntimeError('QUEUE_FULL')
  ident=uuid.uuid4().hex
  c.execute('INSERT INTO jobs(id,kind,status,created) VALUES (?,?,?,?)',(ident,kind,'queued',now()))
 return ident
def report(kind):
 s=monitor.snapshot()
 d=s['database']
 out={'kind':kind,'time':now(),'severity':s['severity'],
      'errors':s['errors'],'warnings':s['warnings'],
      'pending':d.get('pending_count'),'positions':d.get('positions'),
      'closed':d.get('closed_trades'),'restricted_pairs':d.get('restricted_pairs',[]),
      'actions_executed':[],'codex_executed':False}
 if kind=='campaign':
  with sqlite3.connect('file:'+str(monitor.DB)+'?mode=ro',uri=True,timeout=5) as c:
   n,pnl,wins=c.execute('SELECT count(*),coalesce(sum(pnl),0),coalesce(sum(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) FROM closed_trades').fetchone()
  out.update({'closed':n,'target':100,'realized_pnl_usdt':round(pnl,5),'wins':wins})
 if kind=='triage':
  out['recommendation']=('READ_ONLY_RECONCILIATION_NO_RETRY' if out['pending'] else
    'INSPECT_SERVICE_AND_LOGS' if s['errors'] else 'CONTINUE_MONITORING')
  out['codex']='REQUIRES_SEPARATE_APPROVAL'
 return out
def one():
 with conn() as c:
  c.execute('BEGIN IMMEDIATE')
  row=c.execute("SELECT id,kind FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
  if row is None:return None
  ident,kind=row['id'],row['kind']
  c.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'",(ident,))
 try:
  data=report(kind)
  state='needs_review' if data['severity'] in ('WARNING','CRITICAL') or data.get('pending',0) else 'done'
  result=json.dumps(data,ensure_ascii=False)
 except Exception as e:
  state='uncertain';result=json.dumps({'error_type':type(e).__name__,'no_auto_retry':True})
 with conn() as c:
  c.execute("UPDATE jobs SET status=?,finished=?,result=? WHERE id=? AND status='running'",(state,now(),result,ident))
 return {'job_id':ident,'type':kind,'status':state}
def summary():
 with conn() as c:
  totals=dict(c.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
  latest=[dict(r) for r in c.execute('SELECT kind,status,created,finished,result FROM jobs ORDER BY created DESC LIMIT 3')]
 for j in latest:
  if j['result']:j['result']=json.loads(j['result'])
 return {'mode':'READ_ONLY_DIAGNOSTICS','auto_codex':False,'auto_trading':False,
         'counts':totals,'recent':latest}
def scheduled(kind,age_seconds):
 from datetime import timedelta
 with conn() as c:
  r=c.execute('SELECT created FROM jobs WHERE kind=? ORDER BY created DESC LIMIT 1',(kind,)).fetchone()
  if r and (datetime.now(timezone.utc)-datetime.fromisoformat(r[0])).total_seconds()<age_seconds:
   return False
 try:
  add(kind)
  return True
 except RuntimeError as exc:
  if str(exc)=='QUEUE_FULL':return False
  raise
def daemon():
 if LOCK.is_symlink():raise RuntimeError('UNSAFE_LOCK')
 with LOCK.open('a+') as lock:
  fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  os.chmod(LOCK,0o600)
  with conn() as c:
   c.execute("UPDATE jobs SET status='uncertain',finished=? WHERE status='running'",(now(),))
  while True:
   try:
    scheduled('health',3600)
    scheduled('campaign',21600)
    state=monitor.read_state()
    if state.get('severity') in ('WARNING','CRITICAL'):scheduled('triage',3600)
    for _ in range(12):
     if one() is None:break
   except Exception as exc:
    print('AUTOPILOT_QUEUE_EXCEPTION',type(exc).__name__,flush=True)
   time.sleep(60)
def guarded_one():
 if LOCK.is_symlink():raise RuntimeError('UNSAFE_LOCK')
 with LOCK.open('a+') as lock:
  try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise RuntimeError('DAEMON_OWNS_QUEUE')
  return one()
def main():
 parser=argparse.ArgumentParser()
 x=parser.add_mutually_exclusive_group(required=True)
 x.add_argument('--enqueue',choices=TYPES);x.add_argument('--once',action='store_true')
 x.add_argument('--status',action='store_true');x.add_argument('--daemon',action='store_true')
 a=parser.parse_args()
 if os.getuid()!=1001:raise RuntimeError('WRONG_UID')
 if a.enqueue:print('QUEUED',a.enqueue,add(a.enqueue))
 elif a.once:print(json.dumps(guarded_one() or {'empty':True}))
 elif a.status:print(json.dumps(summary(),ensure_ascii=False))
 else:daemon()
if __name__=='__main__':main()


#!/usr/bin/env python3
"""LUK Autopilot Codex gateway: bounded autonomous DIAGNOSIS, never deployments."""
from __future__ import annotations
from datetime import datetime,timezone
from contextlib import contextmanager
from pathlib import Path
import hashlib,json,os,sqlite3,sys,uuid

ROOT=Path(__file__).resolve().parent
DB=ROOT/'intelligence.sqlite3'
BRIDGE=ROOT.parent
BRIDGE_DB=BRIDGE/'bridge.sqlite3'
ELIGIBLE=frozenset({
    'V2_SERVICE_DOWN','MCP_BRIDGE_DOWN','V2_CYCLES_STALE','V2_CYCLES_NOT_SEEN',
    'V2_UNRESOLVED_ORDERS_OLDER_10_MIN','V2_DATABASE_UNVERIFIED',
})
DAY_CAP=1
MIN_AGE_SECONDS=600
DAY_SECONDS=86400

def when():return datetime.now(timezone.utc)
def stamp():return when().isoformat()
def secs(iso):
 try:return (when()-datetime.fromisoformat(iso)).total_seconds()
 except (TypeError,ValueError):return 0

@contextmanager
def connection():
 if DB.is_symlink() or BRIDGE_DB.is_symlink():raise RuntimeError('UNSAFE_DB_LINK')
 db=sqlite3.connect(str(DB),timeout=10)
 db.row_factory=sqlite3.Row
 db.execute('PRAGMA busy_timeout=10000')
 db.execute("""CREATE TABLE IF NOT EXISTS incidents(
     fingerprint TEXT PRIMARY KEY,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
     observations INTEGER NOT NULL,claimed_at TEXT,bridge_task_id TEXT,
     status TEXT NOT NULL)""")
 db.execute("""CREATE TABLE IF NOT EXISTS daily_quota(
     day_utc TEXT PRIMARY KEY,claims INTEGER NOT NULL)""")
 if 'last_snapshot_at' not in {x[1] for x in db.execute('PRAGMA table_info(incidents)')}:
  db.execute('ALTER TABLE incidents ADD COLUMN last_snapshot_at TEXT')
 os.chmod(DB,0o600)
 try:
  with db:yield db
 finally:db.close()

def fresh(report):
 try:
  value=report.get('time_utc')
  parsed=datetime.fromisoformat(value)
  if parsed.tzinfo is None:return False
  age=(when()-parsed).total_seconds()
  return -60<=age<=900
 except (AttributeError,TypeError,ValueError):return False

def codes(report):
 if not isinstance(report,dict) or report.get('severity')!='CRITICAL':return ()
 result=[]
 for err in report.get('errors',[]):
  if not isinstance(err,str):continue
  prefix=err.split(':',1)[0]
  if prefix in ELIGIBLE:result.append(prefix)
 return tuple(sorted(set(result)))

def bridge_idle():
 if BRIDGE_DB.is_symlink() or not BRIDGE_DB.is_file():return False
 with sqlite3.connect('file:'+str(BRIDGE_DB)+'?mode=ro',uri=True,timeout=5) as c:
  return c.execute("SELECT count(*) FROM tasks WHERE status IN ('pending','running','uncertain')").fetchone()[0]==0

def prompt(report,incident):
 db=report.get('database',{}) if isinstance(report.get('database'),dict) else {}
 services=report.get('services',{}) if isinstance(report.get('services'),dict) else {}
 compact={'alert_codes':list(incident),'service_health':
          {k:bool(v.get('active')) for k,v in services.items()
           if k in ('trade','telegram_menu','bridge','notifier') and isinstance(v,dict)},
         'db_integrity':'ok' if db.get('integrity')=='ok' else 'UNVERIFIED',
         'pending_count':int(db.get('pending_count',0) or 0),
         'closed_trades':int(db.get('closed_trades',0) or 0),
         'quarantined_count':int(db.get('quarantined_count',0) or 0)}
 evidence=json.dumps(compact,ensure_ascii=True,sort_keys=True)
 return (
 'LUK_AUTOPILOT_READONLY_V2\n'
 'LUK AUTOPILOT V2 — DIAGNOSTICO AUTOMATICO READ_ONLY. '
 'Trabalhe apenas com os indicadores fornecidos. Nao leia credenciais, '
 'nao execute comandos nem modifique arquivos/servicos, nao envie ordens, '
 'nao produza codigo executavel. Nao tente contornar restricoes OKX. '
 'Sua resposta deve conter: 1) classificacao do risco, 2) verificacoes '
 'somente-leitura sugeridas, 3) causa possivel (nao confirmada), '
 '4) proposta de correcao para aprovacao humana, 5) GO/NO_GO. '
 'Nao ha permissao de deploy, restart, vendas ou compras. '
 'Nao confunda processo ativo com operacao comprovada. '
 'Maximo 1000 caracteres de resposta. INDICADORES='+evidence)

def submit_readonly_to_bridge(message):
 # Atomic with normal Bridge enqueue operations: a concurrent unrelated job
 # cannot slip between our queue-idle check and our INSERT.
 sys.path.insert(0,str(BRIDGE))
 from config import DB_PATH,MAX_TASK_PROMPT_BYTES
 if Path(DB_PATH).resolve()!=BRIDGE_DB.resolve():
  raise RuntimeError('BRIDGE_DB_PATH_MISMATCH')
 if (not message.startswith('LUK_AUTOPILOT_READONLY_V2\n')
     or len(message.encode('utf-8'))>MAX_TASK_PROMPT_BYTES):
  raise RuntimeError('INVALID_AUTOPILOT_DIAGNOSTIC')
 ident=uuid.uuid4().hex
 import contextlib
 with contextlib.closing(sqlite3.connect(str(BRIDGE_DB),timeout=10)) as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute("SELECT 1 FROM tasks WHERE status IN ('pending','running','uncertain') LIMIT 1").fetchone():
   c.rollback()
   raise RuntimeError('BRIDGE_BUSY')
  now=datetime.now(timezone.utc).isoformat(timespec='seconds')
  c.execute("""INSERT INTO tasks(id,action,prompt,status,created_at,updated_at)
               VALUES (?,'self_test',,'pending',,?)""",(ident,message,now,now))
  c.commit()
 return ident

def tick(report,submit=None):
 inc=codes(report)
 if not fresh(report):return {'status':'STALE_MONITOR_REPORT_NO_ACTION'}
 if not inc:
  # A healthy fresh snapshot resets only never-dispatched observations.
  with connection() as c:
   c.execute("UPDATE incidents SET status='recovered',observations=0,last_snapshot_at=NULL WHERE status='observing'")
  return {'status':'NO_ELIGIBLE_CRITICAL_ERROR'}
 fingerprint=hashlib.sha256(('|'.join(inc)).encode()).hexdigest()
 with connection() as c:
  c.execute('BEGIN IMMEDIATE')
  existing=c.execute('SELECT * FROM incidents WHERE fingerprint=?',(fingerprint,)).fetchone()
  if existing is None:
   c.execute("""INSERT INTO incidents(fingerprint,first_seen,last_seen,observations,status,last_snapshot_at)
                VALUES (?,?,,?,?,)""",(fingerprint,stamp(),stamp(),1,'observing',report['time_utc']))
   return {'status':'OBSERVING_CRITICAL','observations':1}
  if existing['status'] in ('reserved',) or existing['status'].startswith('submission_uncertain'):
   return {'status':'UNVERIFIED_SUBMISSION_REQUIRES_MANUAL_RECONCILIATION'}
  if existing['claimed_at'] and secs(existing['claimed_at'])<DAY_SECONDS:
   return {'status':'COOLDOWN'}
  if existing['status']=='recovered':
   c.execute("""UPDATE incidents SET first_seen=?,last_seen=?,observations=1,
                 status='observing',last_snapshot_at= WHERE fingerprint=?""",
             (stamp(),stamp(),report['time_utc'],fingerprint))
   return {'status':'OBSERVING_CRITICAL','observations':1}
  if report['time_utc']!=existing['last_snapshot_at']:
   c.execute("""UPDATE incidents SET observations=observations+1,last_seen=?,last_snapshot_at=
                WHERE fingerprint=""",(stamp(),report['time_utc'],fingerprint))
  else:
   return {'status':'REPEATED_SNAPSHOT_IGNORED'}
  if secs(existing['first_seen'])<MIN_AGE_SECONDS or existing['observations']<1:
   return {'status':'OBSERVING_CRITICAL','observations':existing['observations']}
  if existing['claimed_at'] and secs(existing['claimed_at'])<DAY_SECONDS:
   return {'status':'COOLDOWN'}
  day=when().date().isoformat()
  used=c.execute('SELECT claims FROM daily_quota WHERE day_utc=',(day,)).fetchone()
  if used and used['claims']>=DAY_CAP:return {'status':'DAILY_QUOTA_REACHED'}
  if not bridge_idle():return {'status':'BRIDGE_BUSY_OR_UNCERTAIN'}
  # Consume quota BEFORE contacting external queue. A crash safely burns quota,
  # never silently duplicates an AI task or retries a possibly submitted task.
  c.execute("""INSERT INTO daily_quota(day_utc,claims) VALUES (,1)
               ON CONFLICT(day_utc) DO UPDATE SET claims=claims+1""",(day,))
  c.execute("""UPDATE incidents SET claimed_at=,status='reserved',last_seen=
               WHERE fingerprint=?""",(stamp(),stamp(),fingerprint))
 try:
  if submit is None:task=submit_readonly_to_bridge(prompt(report,inc))
  else:task=submit(prompt(report,inc))
  if not isinstance(task,str) or len(task)!=32:raise RuntimeError('TASK_RESULT_UNVERIFIED')
 except Exception as exc:
  with connection() as c:
   c.execute("UPDATE incidents SET status=? WHERE fingerprint=",
             ('submission_uncertain_'+type(exc).__name__,fingerprint))
  return {'status':'SUBMISSION_UNCERTAIN_NO_RETRY'}
 with connection() as c:
  c.execute("""UPDATE incidents SET bridge_task_id=?,status='queued_codex'
               WHERE fingerprint=?""",(task,fingerprint))
 return {'status':'CODEX_DIAGNOSIS_QUEUED','task_id':task}

def status():
 with connection() as c:
  incidents=[dict(r) for r in c.execute(
     'SELECT fingerprint,first_seen,last_seen,observations,claimed_at,bridge_task_id,status FROM incidents ORDER BY last_seen DESC LIMIT 5')]
  daily=dict(c.execute('SELECT day_utc,claims FROM daily_quota ORDER BY day_utc DESC LIMIT 3'))
 return {'enabled':True,'mode':'DIAGNOSIS_READONLY_ONLY','daily_cap':DAY_CAP,
         'automatic_deploy':False,'automatic_trading':False,
         'recent_incidents':incidents,'quota':daily}


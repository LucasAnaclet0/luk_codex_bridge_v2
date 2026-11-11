#!/usr/bin/env python3
"""LUK Autopilot V2: OBSERVE_ONLY. Never edits trading DB or starts orders."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT=Path('/home/ubuntu/luk_codex_bridge_v2/luk_autopilot_v2')
TRADER=Path('/home/ubuntu/luk_bot_trader_v2')
DB=TRADER/'data/v2_okx_demo.sqlite3'
LOG=TRADER/'logs/v2_demo.log'
STATE=ROOT/'state.json'
LOCK=ROOT/'autopilot.lock'
INTERVAL=300
STOP=False
SERVICES={
 'trade':'luk-bot-trader-v2.service',
 'telegram_menu':'luk-bot-trader-v2-telegram-menu.service',
 'bridge':'luk-codex-mcp.service',
 'notifier':'luk-codex-notifier.service',
}
def ensure_dir():
 if ROOT.is_symlink():raise RuntimeError('AUTOPILOT_PATH_SYMLINK')
 ROOT.mkdir(mode=0o700,exist_ok=True)
 os.chmod(ROOT,0o700)

def atomic_json(path,payload):
 if path.is_symlink():raise RuntimeError('AUTOPILOT_STATE_SYMLINK')
 fd,temp=tempfile.mkstemp(dir=str(ROOT),prefix='.state-',suffix='.tmp')
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:
   os.fchmod(f.fileno(),0o600)
   json.dump(payload,f,ensure_ascii=False,sort_keys=True,indent=2)
   f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(temp,path)
 finally:
  if os.path.exists(temp):os.unlink(temp)

def read_state():
 try:
  if STATE.is_symlink():raise RuntimeError('STATE_SYMLINK')
  return json.loads(STATE.read_text()) if STATE.is_file() else {}
 except (ValueError,OSError):return {}

def service_state(unit):
 proc=subprocess.run(['/usr/bin/systemctl','show',unit,'-p','ActiveState','-p','MainPID',
                      '-p','SubState'],capture_output=True,text=True,timeout=8)
 if proc.returncode:return {'active':False,'pid':0,'status':'UNKNOWN'}
 data=dict(line.split('=',1) for line in proc.stdout.splitlines() if '=' in line)
 try:pid=int(data.get('MainPID','0'))
 except ValueError:pid=0
 return {'active':data.get('ActiveState')=='active',
         'pid':pid,'status':data.get('SubState','UNKNOWN')}

def inspect_db():
 if DB.is_symlink() or not DB.is_file():raise RuntimeError('DB_MISSING_OR_SYMLINK')
 with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=7) as c:
  if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':
   raise RuntimeError('DB_INTEGRITY_FAILURE')
  rows=dict(c.execute('SELECT status,count(*) FROM orders GROUP BY status'))
  positions=c.execute('SELECT count(*) FROM positions').fetchone()[0]
  closed=c.execute('SELECT count(*) FROM closed_trades').fetchone()[0]
  pending=c.execute("SELECT status,created_at FROM orders WHERE status IN ('PREPARED','OPEN','UNKNOWN')").fetchall()
  quarantine=c.execute("SELECT count(*) FROM orders WHERE status='QUARANTINED'").fetchone()[0]
  control_exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_controls'").fetchone() is not None
  restricted=(c.execute("SELECT value FROM runtime_controls WHERE name='exchange_restricted'").fetchone()
              if control_exists else None)
  exchange_restricted=restricted is not None and restricted[0]=='1'
 return {'orders':rows,'positions':positions,'closed_trades':closed,
         'exchange_restricted':exchange_restricted,
         'pending_count':len(pending),'quarantined_count':quarantine,
         'oldest_pending_age_seconds':max((age_utc(ts) for _,ts in pending),default=0),
         'integrity':'ok'}

def age_utc(iso):
 try:return max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(iso)).total_seconds()))
 except (ValueError,TypeError):return 0

def inspect_logs():
 if LOG.is_symlink() or not LOG.is_file():return {'cycle_age_seconds':None,'recent_errors':0}
 with LOG.open('rb') as f:
  f.seek(0,2);size=f.tell()
  f.seek(max(0,size-262144))
  lines=f.read(262144).decode('utf-8','replace').splitlines()
 cycle_age=None;recent_errors=0
 for line in lines:
  if 'DEMO CYCLE' not in line and ' ERROR ' not in line and ' CRITICAL ' not in line:continue
  match=re.match(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)',line)
  if match is None:continue
  try:
   stamped=datetime.strptime(match.group(1),'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
   age=max(0,int((datetime.now(timezone.utc)-stamped).total_seconds()))
  except ValueError:continue
  if 'DEMO CYCLE' in line:cycle_age=age
  if age<=900 and (' ERROR ' in line or ' CRITICAL ' in line):recent_errors+=1
 return {'cycle_age_seconds':cycle_age,'recent_errors':recent_errors}

def snapshot():
 now=datetime.now(timezone.utc).isoformat()
 services={name:service_state(unit) for name,unit in SERVICES.items()}
 errors=[];warnings=[]
 if not services['trade']['active']:errors.append('V2_SERVICE_DOWN')
 if not services['bridge']['active']:errors.append('MCP_BRIDGE_DOWN')
 for name in ('telegram_menu','notifier'):
  if not services[name]['active']:warnings.append(name.upper()+'_DOWN')
 try:database=inspect_db()
 except (sqlite3.Error,OSError,RuntimeError) as exc:
  database={'integrity':'UNVERIFIED'}
  errors.append('V2_DATABASE_UNVERIFIED:'+type(exc).__name__)
 else:
  if database['pending_count'] and database['oldest_pending_age_seconds']>=600:
   errors.append('V2_UNRESOLVED_ORDERS_OLDER_10_MIN')
  # Evidence-preserving quarantine is informational, not a new operational error.
  if database.get('exchange_restricted'):
   warnings.append('OKX_RESTRICTED_LOCATION_ALL_ORDER_SUBMISSIONS_BLOCKED')
   if database.get('positions',0)>0:
    errors.append('OPEN_POSITIONS_CANNOT_AUTO_EXIT_EXCHANGE_RESTRICTION')
 try:logs=inspect_logs()
 except (OSError,RuntimeError):
  logs={'cycle_age_seconds':None,'recent_errors':0}
  warnings.append('V2_LOGS_UNAVAILABLE')
 if services['trade']['active']:
  if logs['cycle_age_seconds'] is None:errors.append('V2_CYCLES_NOT_SEEN')
  elif logs['cycle_age_seconds']>900:errors.append('V2_CYCLES_STALE')
 if logs['recent_errors']:warnings.append('RECENT_V2_ERRORS:'+str(logs['recent_errors']))
 severity='CRITICAL' if errors else ('WARNING' if warnings else 'OK')
 return {'time_utc':now,'mode':'MONITOR_ONLY','severity':severity,
         'errors':errors,'warnings':warnings,'services':services,
         'database':database,'logs':logs,'trades_executed_by_autopilot':0,
         'mutations_authorized':False}

def alert_message(report):
 s=report['severity']
 reasons=report['errors']+report['warnings']
 metrics=report['database']
 return ('🤖 LUK AUTOPILOT V2 | '+s+'\n'
         'Modo: monitoramento, sem comandos de negociação.\n'
         'Ocorrências: '+(', '.join(reasons) or 'nenhuma')+'\n'
         'Bot: '+('ativo' if report['services']['trade']['active'] else 'inativo')
         +' | Bridge: '+('ativa' if report['services']['bridge']['active'] else 'inativa')+'\n'
         'Campanha: '+str(metrics.get('closed_trades','?'))+'/100'
         ' | Posições: '+str(metrics.get('positions','?'))+'\n'
         'Estado: '+str(STATE))

def notify(report,previous):
 current=report['severity']+'|'+','.join(sorted(report['errors']+report['warnings']))
 before=str(previous.get('notification_key',''))
 sent_at=float(previous.get('notification_epoch',0) or 0)
 changed=current!=before
 repeat=report['severity']=='CRITICAL' and time.time()-sent_at>14400
 if not (changed or repeat):return previous.get('notification_key',''),sent_at,False
 try:
  sys.path.insert(0,str(TRADER))
  from v2.telegram_notify import send_safe
  delivered=bool(send_safe(alert_message(report)))
 except Exception:delivered=False
 return (current if delivered else before,
         time.time() if delivered else sent_at,delivered)

def run_once(no_telegram=False):
 ensure_dir()
 old=read_state()
 report=snapshot()
 notification_key=old.get('notification_key','')
 notification_epoch=old.get('notification_epoch',0)
 sent=False
 if not no_telegram:
  notification_key,notification_epoch,sent=notify(report,old)
 report['notification_key']=notification_key
 report['notification_epoch']=notification_epoch
 report['telegram_sent_this_check']=sent
 atomic_json(STATE,report)
 print(json.dumps({k:v for k,v in report.items()
                   if k not in ('notification_epoch','notification_key')},ensure_ascii=False),flush=True)
 return report

def main():
 global STOP
 parser=argparse.ArgumentParser()
 modes=parser.add_mutually_exclusive_group(required=True)
 modes.add_argument('--once',action='store_true')
 modes.add_argument('--daemon',action='store_true')
 modes.add_argument('--status',action='store_true')
 parser.add_argument('--no-telegram',action='store_true')
 args=parser.parse_args()
 if os.getuid()!=1001:raise RuntimeError('EXPECTED_UBUNTU_UID')
 ensure_dir()
 if args.status:
  print(json.dumps(read_state(),ensure_ascii=False));return
 if args.once:
  run_once(no_telegram=args.no_telegram);return
 if args.no_telegram:raise RuntimeError('DAEMON_NOTIFICATION_REQUIRED')
 def stop(*_):
  global STOP
  STOP=True
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 if LOCK.is_symlink():raise RuntimeError('LOCK_SYMLINK')
 with LOCK.open('a+') as f:
  fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  while not STOP:
   try:run_once()
   except Exception as e:
    print('AUTOPILOT_CHECK_FAILED:'+type(e).__name__,flush=True)
   for _ in range(INTERVAL):
    if STOP:break
    time.sleep(1)
if __name__=='__main__':main()


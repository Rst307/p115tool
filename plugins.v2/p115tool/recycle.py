"""Opt-in daily recycle-bin emptying with durable, explicit batch targets."""
import json
import re
import time
import uuid


class RecycleCleaner:
    def __init__(self, storage):
        self.storage=storage;self.service=storage.service;self.client=storage.client
        self.service.query('''CREATE TABLE IF NOT EXISTS recycle_empty_runs (
            id TEXT PRIMARY KEY,stage TEXT NOT NULL,targets TEXT NOT NULL,inflight TEXT NOT NULL,
            total INTEGER NOT NULL,done INTEGER NOT NULL,updated_at REAL NOT NULL,error TEXT NOT NULL)''')
        self.service.query("UPDATE recycle_empty_runs SET stage='UNKNOWN',error='RECYCLE_EMPTY_UNKNOWN' WHERE stage='PURGING'")
        # PREPARED is written only before any request or after a verified batch.
        self.service.query("UPDATE recycle_empty_runs SET stage='CANCELLED' WHERE stage='PREPARED'")

    def pending(self):
        return self.service.query("SELECT * FROM recycle_empty_runs WHERE stage IN ('PURGING','UNKNOWN') ORDER BY updated_at LIMIT 1",one=True)

    def status(self):
        row=self.pending() or self.service.query('SELECT * FROM recycle_empty_runs ORDER BY updated_at DESC LIMIT 1',one=True)
        cfg=self.storage.config
        return {'scheduled':cfg.recycle_empty_scheduled,'time':cfg.recycle_empty_time,
                'stage':row['stage'] if row else 'IDLE','done':row['done'] if row else 0,
                'total':row['total'] if row else 0,'updated_at':row['updated_at'] if row else None,
                'blocked':bool(row and row['stage'] in ('PURGING','UNKNOWN')),
                'error':row['error'] if row else ''}

    def save(self, run, stage, error=''):
        value={**run,'stage':stage,'error':error,'updated_at':time.time()}
        self.service.query('INSERT OR REPLACE INTO recycle_empty_runs VALUES(?,?,?,?,?,?,?,?)',
            tuple(value[k] for k in ('id','stage','targets','inflight','total','done','updated_at','error')))
        run.update(value)

    def reconcile(self, job):
        # Only absence of ALL persisted inflight IDs clears an unknown result.
        # Remaining snapshot IDs are never resumed from an old run.
        with self.storage._gate:
            run=self.pending()
            if not run: return
            job['phase']='RECYCLE_RECHECK';self.storage._progress(job)
            try:
                inflight=json.loads(run['inflight'])
                if (not isinstance(inflight,list) or not inflight or len(inflight)>100
                        or any(not isinstance(rid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',rid) for rid in inflight)):
                    raise ValueError('Invalid persisted targets')
                present={entry['rid'] for entry in self.client.recycle_entries()}
                if present.intersection(inflight): raise ValueError('Outcome not verified')
                run['done']+=len(inflight)
                self.save(run,'VERIFIED')
                job['done']=len(inflight)
            except Exception:
                job['failed']+=1;job['error']='RECYCLE_EMPTY_UNKNOWN'

    def empty(self, job):
        with self.storage._gate:
            cfg=self.storage.config
            if not cfg.enabled or not cfg.recycle_empty_scheduled or self.service._stop.is_set(): return
            if self.pending():
                # A subsequent timer tick is read-only, even if it resolves the
                # previous batch. No destructive retry in the same request.
                self.reconcile(job)
                return
            run={'id':uuid.uuid4().hex,'targets':'[]','inflight':'[]','total':0,'done':0}
            if self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True):
                self.save(run,'BLOCKED','RECYCLE_EMPTY_BLOCKED')
                job['failed']+=1;job['error']='RECYCLE_EMPTY_BLOCKED';return
            try:
                if not re.fullmatch(r'[0-9]{6}',cfg.recycle_password): raise ValueError('Security key required')
                job['phase']='RECYCLE_EMPTY_SNAPSHOT';self.storage._progress(job)
                targets=[entry['rid'] for entry in self.client.recycle_entries()]
                if (len(targets)>100000 or len(set(targets))!=len(targets)
                        or any(not re.fullmatch(r'[1-9][0-9]{0,19}',rid) for rid in targets)):
                    raise ValueError('Invalid snapshot')
                run['targets']=json.dumps(targets);run['total']=len(targets)
                job['total']=len(targets)
                self.save(run,'PREPARED')
                for offset in range(0,len(targets),100):
                    if self.service._stop.is_set() or not cfg.recycle_empty_scheduled:
                        self.save(run,'CANCELLED');raise InterruptedError()
                    batch=targets[offset:offset+100]
                    run['inflight']=json.dumps(batch)
                    self.save(run,'PURGING')
                    job['phase']='RECYCLE_EMPTY_PURGING';self.storage._progress(job)
                    self.client.purge_recycle_batch(batch)
                    job['phase']='RECYCLE_RECHECK';self.storage._progress(job)
                    present={entry['rid'] for entry in self.client.recycle_entries()}
                    if present.intersection(batch): raise ValueError('Outcome not verified')
                    run['done']+=len(batch);run['inflight']='[]'
                    self.save(run,'PREPARED')
                    job['done']=run['done'];self.storage._progress(job)
                self.save(run,'DONE')
            except InterruptedError:
                if run.get('stage')=='PURGING': self.save(run,'UNKNOWN','RECYCLE_EMPTY_UNKNOWN')
                raise
            except Exception:
                persisted=self.service.query('SELECT * FROM recycle_empty_runs WHERE id=?',(run['id'],),one=True)
                unknown=bool(persisted and persisted['stage']=='PURGING')
                if unknown: run=persisted
                error='RECYCLE_EMPTY_UNKNOWN' if unknown else 'RECYCLE_EMPTY_FAILED'
                self.save(run,'UNKNOWN' if unknown else 'FAILED',error)
                job['failed']+=1;job['error']=error

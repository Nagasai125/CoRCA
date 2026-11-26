"""Human-confirmed incident outcomes, kept separate from published CI reports."""
import hashlib
import json
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.access import audit
from corca.metrics import set_prf


class OutcomeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    outcome: Literal['fault', 'healthy', 'unconfirmed']
    root_causes: list[str] = Field(default_factory=list, max_length=100)
    evidence_kind: Literal['real_incident', 'controlled_drill', 'synthetic']
    evidence: str = Field(min_length=10, max_length=1000)

    @model_validator(mode='after')
    def consistent(self):
        self.evidence = self.evidence.strip()
        self.root_causes = [name.strip() for name in self.root_causes]
        if len(self.evidence)<10 or any(not name or len(name)>200 for name in self.root_causes):
            raise ValueError('Provide service names and independent confirmation evidence')
        if len(set(self.root_causes))!=len(self.root_causes):
            raise ValueError('Cause names must be unique')
        if bool(self.root_causes)!=(self.outcome=='fault'):
            raise ValueError('Confirmed faults require causes; healthy or unconfirmed windows require none')
        return self


def prediction(job):
    result = json.loads(job['result']) if job['result'] else {}
    available = job['status']=='done' and result.get('status') in (
        'diagnosed', 'partial_unexplained', 'partial_timeout', 'no_significant_anomaly')
    roots = result.get('root_causes')
    available = available and isinstance(roots, list) and all(isinstance(s,str) and s for s in roots)
    # A timed-out search with no candidates is not evidence of a quiet window.
    available = available and (not roots if result.get('status')=='no_significant_anomaly' else bool(roots))
    return result, roots if available else [], bool(available)


def cohort(job, evidence_kind):
    # Hash the saved configuration; never expose provider credentials in summaries.
    context = json.loads(job['context']) if job['context'] else {'unknown_configuration_for':job['id']}
    if context.get('analysis_profile'):
        context = {'analysis_profile': context['analysis_profile']}
    settings = [job['method'], context, job['inject_time']-job['start'], job['end']-job['inject_time']]
    key = hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()[:16]
    return key+':'+evidence_kind


class Reviews:
    def __init__(self, jobs):
        self.jobs = jobs
        with jobs._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS diagnosis_reviews (
                job_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                reviewer TEXT NOT NULL, reviewed_at REAL NOT NULL)''')

    @staticmethod
    def view(row):
        return {**json.loads(row['payload']), 'revision':row['revision'],
                'reviewer':row['reviewer'], 'reviewed_at':row['reviewed_at']}

    def get(self, identifier):
        with self.jobs._db() as db:
            if not db.execute('SELECT 1 FROM jobs WHERE id=?',(identifier,)).fetchone():
                raise HTTPException(404,'Diagnosis not found')
            row = db.execute('SELECT * FROM diagnosis_reviews WHERE job_id=?',(identifier,)).fetchone()
        return self.view(row) if row else None

    def save(self, identifier, body, actor):
        with self.jobs._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            if job is None: raise HTTPException(404,'Diagnosis not found')
            if job['status'] not in ('done','failed'):
                raise HTTPException(409,'Wait for the diagnosis to finish before reviewing its outcome')
            old = db.execute('SELECT * FROM diagnosis_reviews WHERE job_id=?',(identifier,)).fetchone()
            if body.revision != (old['revision'] if old else 0):
                raise HTTPException(409,'Another reviewer changed this outcome. Reload before saving.')
            revision = body.revision+1
            payload = body.model_dump(exclude={'revision'})
            db.execute('INSERT OR REPLACE INTO diagnosis_reviews VALUES (?,?,?,?,?)',
                       (identifier,revision,json.dumps(payload),actor['name'],time.time()))
            audit(db,actor,'diagnosis.reviewed',identifier,{'revision':revision,
                  'previous':json.loads(old['payload']) if old else None, 'review':payload})
        return self.get(identifier)

    def summary(self, before=None):
        groups = {}; seen = set(); duplicates = 0
        with self.jobs._db() as db:
            reviewed = db.execute('''SELECT j.*, r.payload, r.reviewed_at FROM jobs j JOIN diagnosis_reviews r
                ON j.id=r.job_id ORDER BY r.reviewed_at DESC, j.id DESC''')
            for job in reviewed:
                review = json.loads(job['payload'])
                if review['outcome']=='unconfirmed': continue
                key = cohort(job,review['evidence_kind'])
                incident = (key,job['inject_time'])
                if incident in seen:
                    duplicates += 1; continue
                seen.add(incident)
                context = json.loads(job['context']) if job['context'] else {}
                source = context.get('source') or {}
                group = groups.setdefault(key,{'id':key,'method':job['method'],
                    'threshold_mode':context.get('threshold_mode','unknown'),
                    'tau':(context.get('thresholds') or {}).get(job['method']),
                    'provider':source.get('provider','prometheus') if source else 'unknown',
                    'normal_seconds':job['inject_time']-job['start'],
                    'anomaly_seconds':job['end']-job['inject_time'],
                    'evidence_kind':review['evidence_kind'], 'cases':0,'faults':0,'healthy':0,
                    'tp':0,'fp':0,'tn':0,'fn':0,'unavailable':0,'exact':0,'f1_total':0,'case_details':[]})
                result, roots, available = prediction(job)
                if len(group['case_details'])<1000: group['case_details'].append({'case_id':job['id'],'diagnosis_id':job['id'],
                    'fault_type':'Unclassified','outcome':review['outcome'],'truth':review['root_causes'],
                    'predicted':roots,'status':result.get('status',job['status']) if available else 'failed',
                    'seconds':result.get('time_to_result_seconds'), 'incident_time':job['inject_time']})
                fault = review['outcome']=='fault'
                group['cases'] += 1; group['faults' if fault else 'healthy'] += 1
                if not available: group['unavailable'] += 1
                else: group['tp' if fault and roots else 'fn' if fault else 'fp' if roots else 'tn'] += 1
                if fault:
                    group['exact'] += int(available and result['status']=='diagnosed' and set(roots)==set(review['root_causes']))
                    group['f1_total'] += set_prf(roots, review['root_causes'])[2]
            pending_where = "j.status IN ('done','failed') AND (r.job_id IS NULL OR json_extract(r.payload,'$.outcome')='unconfirmed')"
            pending_count = db.execute('SELECT count(*) FROM jobs j LEFT JOIN diagnosis_reviews r ON j.id=r.job_id WHERE '+pending_where).fetchone()[0]
            pending = db.execute('SELECT j.rowid AS cursor,j.* FROM jobs j LEFT JOIN diagnosis_reviews r ON j.id=r.job_id WHERE '+pending_where+' AND j.rowid<? ORDER BY j.rowid DESC LIMIT 21',
                                 (before if before is not None else 2**63-1,)).fetchall()
            recent = db.execute('SELECT * FROM diagnosis_reviews ORDER BY reviewed_at DESC LIMIT 20').fetchall()
        for group in groups.values():
            def metric(name, numerator, denominator):
                return {'name':name,'value':numerator/denominator if denominator else None,'samples':denominator}
            group['metrics'] = [metric('complete_exact_match',group['exact'],group['faults']),
                metric('mean_f1',group.pop('f1_total'),group['faults']),
                metric('detection_recall',group['tp'],group['tp']+group['fn']),
                metric('healthy_false_positive',group['fp'],group['fp']+group['tn']),
                metric('unavailable_rate',group['unavailable'],group['cases'])]
        return {'cohorts':list(groups.values()), 'pending_count':pending_count,
                'pending':[self.jobs.view(row) for row in pending[:20]],
                'next_before':pending[19]['cursor'] if len(pending)>20 else None,
                'duplicate_windows_excluded':duplicates,
                'recent':[{'job_id':row['job_id'],**self.view(row)} for row in recent]}

"""Immutable, publisher-attributed evaluation summaries for the customer console."""
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Metric(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    name: Literal['complete_exact_match','healthy_false_positive','mean_f1','unavailable_rate',
                  'pass_rate','source_correctness','unsupported_claim_rate','response_seconds']
    value: float | None = Field(ge=0,strict=True)
    samples: int = Field(ge=0,le=10_000_000,strict=True)

    @model_validator(mode='after')
    def valid_value(self):
        if self.samples==0 and self.value is not None:
            raise ValueError('Metrics without samples must be null')
        if self.name!='response_seconds' and self.value is not None and self.value>1:
            raise ValueError('Rates must be between zero and one')
        return self


class Gate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=100)
    status: Literal['pass','fail','not_run']
    criterion: str = Field(min_length=1,max_length=300)

    @field_validator('name','criterion')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('Use a nonblank gate name and criterion')
        return value


class Evaluation(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    schema_version: Literal[1] = 1
    run_key: str = Field(min_length=1,max_length=160,pattern=r'^[A-Za-z0-9._:/-]+$')
    kind: Literal['diagnosis','help','release']
    name: str = Field(min_length=1,max_length=160)
    completed_at: datetime
    source_revision: str | None = Field(default=None,pattern=r'^[a-fA-F0-9]{7,64}$')
    evidence_kind: Literal['synthetic','controlled_drill','real_incident','customer_review','software_test']
    scope: str = Field(min_length=1,max_length=300)
    case_count: int = Field(ge=0,le=10_000_000,strict=True)
    metrics: list[Metric] = Field(default_factory=list,max_length=8)
    gates: list[Gate] = Field(default_factory=list,max_length=30)
    limitations: list[str] = Field(min_length=1,max_length=8)
    pipeline_url: str | None = Field(default=None,max_length=2048)

    @field_validator('completed_at')
    @classmethod
    def timestamp(cls,value):
        if value.tzinfo is None or not 0<value.timestamp()<=time.time()+300:
            raise ValueError('Use a timezone-aware completion time, not a future run')
        return value.astimezone(timezone.utc)

    @field_validator('limitations')
    @classmethod
    def notes(cls,value):
        if any(not note.strip() or len(note)>500 for note in value):
            raise ValueError('Use nonblank limitations of at most 500 characters')
        return value

    @field_validator('pipeline_url')
    @classmethod
    def link(cls,value):
        if value is not None:
            url=urlsplit(value)
            if any(c.isspace() for c in value) or url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Use a final HTTPS pipeline URL without credentials, query, or fragment')
            _=url.port
        return value

    @model_validator(mode='after')
    def evidence(self):
        if not self.name.strip() or not self.scope.strip():
            raise ValueError('Name and scope must be nonblank')
        if len({m.name for m in self.metrics})!=len(self.metrics):
            raise ValueError('Each metric may appear only once')
        if any(m.samples>self.case_count for m in self.metrics):
            raise ValueError('Metric samples cannot exceed the reported case count')
        if self.kind=='diagnosis' and self.evidence_kind not in ('synthetic','controlled_drill','real_incident'):
            raise ValueError('Diagnosis evidence must identify incidents or controlled/synthetic cases')
        if self.kind=='release' and self.evidence_kind!='software_test':
            raise ValueError('Release checks are software tests, not incident-accuracy evidence')
        if self.case_count==0 and any(g.status=='pass' for g in self.gates):
            raise ValueError('An evaluation without cases cannot pass a declared gate')
        allowed={'diagnosis':{'complete_exact_match','healthy_false_positive','mean_f1','unavailable_rate','response_seconds'},
                 'help':{'pass_rate','source_correctness','unsupported_claim_rate','unavailable_rate','response_seconds'},
                 'release':{'pass_rate','response_seconds'}}
        if any(m.name not in allowed[self.kind] for m in self.metrics):
            raise ValueError('Metric does not belong to this evaluation type')
        return self


class Evaluations:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS evaluation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, completed_at REAL NOT NULL, payload TEXT NOT NULL,
                report_sha256 TEXT NOT NULL, publisher_id TEXT NOT NULL, publisher_name TEXT NOT NULL,
                received_at REAL NOT NULL)''')

    def publish(self,report,actor):
        from runtime.access import audit
        encoded=json.dumps(report.model_dump(mode='json'),sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.jobs._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id,report_sha256 FROM evaluation_runs WHERE run_key=?',(report.run_key,)).fetchone()
            if existing:
                if existing['report_sha256']!=digest:
                    raise HTTPException(409,'This run key already has different evidence; publish a new run key')
                return {'id':existing['id'],'duplicate':True}
            cursor=db.execute('INSERT INTO evaluation_runs (run_key,kind,completed_at,payload,report_sha256,publisher_id,publisher_name,received_at) VALUES (?,?,?,?,?,?,?,?)',
                (report.run_key,report.kind,report.completed_at.timestamp(),encoded,digest,actor['id'],actor['name'],time.time()))
            audit(db,actor,'evaluation.published',str(cursor.lastrowid),{'kind':report.kind,'run_key':report.run_key,'report_sha256':digest})
            return {'id':cursor.lastrowid,'duplicate':False}

    def recent(self,kind='diagnosis',before=None,limit=20):
        where='id<?';params=[before if before is not None else 2**63-1]
        if kind!='all':
            where+=' AND kind=?';params.append(kind)
        with self.jobs._db() as db:
            rows=db.execute('SELECT * FROM evaluation_runs WHERE '+where+' ORDER BY id DESC LIMIT ?',[*params,limit]).fetchall()
        result=[]
        for row in rows:
            payload=json.loads(row['payload'])
            status='failed' if any(g['status']=='fail' for g in payload['gates']) else 'incomplete' if any(g['status']=='not_run' for g in payload['gates']) else 'passed' if payload['gates'] else 'not_assessed'
            result.append({**payload,'id':row['id'],'received_at':row['received_at'],'publisher':row['publisher_name'],'report_sha256':row['report_sha256'],'gate_status':status})
        return {'runs':result,'next_before':rows[-1]['id'] if len(rows)==limit else None}


def from_pilot(raw, method, **metadata):
    """Reuse the pilot scorer; never recompute results or retain incident identities."""
    if raw.get('kind')!='pilot_case_assessment' or method not in ('baro','circa'):
        raise ValueError('Use an existing pilot case assessment and a supported method')
    stats=raw['by_method'][method]
    count=stats['cases']
    if (type(count) is not int or count<1 or any(type(stats[key]) is not int or stats[key]<0 for key in ('fault_cases','healthy_cases','evaluable_healthy_cases','unavailable'))
            or stats['fault_cases']+stats['healthy_cases']!=count or stats['evaluable_healthy_cases']>stats['healthy_cases'] or stats['unavailable']>count):
        raise ValueError('The assessment has inconsistent case counts')
    measurements=[('complete_exact_match',stats['complete_fault_exact_match_rate'],stats['fault_cases']),
                  ('healthy_false_positive',stats['healthy_false_positive_rate'],stats['evaluable_healthy_cases']),
                  ('mean_f1',stats['fault_mean_F1'],stats['fault_cases']),
                  ('unavailable_rate',stats['unavailable']/count,count)]
    return Evaluation(kind='diagnosis',evidence_kind=raw['evidence_kind'],case_count=count,
                      metrics=[Metric(name=name,value=value,samples=samples) for name,value,samples in measurements],
                      limitations=['Labels and evidence type are declared by the evaluation owner, not independently verified.',
                                   'Unavailable fault cases remain in fault-score denominators; partial results are not complete exact matches.',
                                   'Healthy false-positive rate uses evaluable healthy cases; inspect unavailable rate separately.',
                                   'No automatic accuracy gate or production approval. Compare against criteria agreed before evaluation.'],**metadata)


def from_junit(path, **metadata):
    import xml.etree.ElementTree as ET
    raw=path.read_bytes()
    if len(raw)>4*1024*1024 or b'<!DOCTYPE' in raw.upper():
        raise ValueError('Use a JUnit report up to 4 MB without document types')
    cases=list(ET.fromstring(raw).iter('testcase'))
    if not cases:
        raise ValueError('JUnit report has no test cases')
    failed=sum(any(case.find(tag) is not None for tag in ('failure','error')) for case in cases)
    skipped=sum(case.find('skipped') is not None and not any(case.find(tag) is not None for tag in ('failure','error')) for case in cases)
    passed=len(cases)-failed-skipped
    return Evaluation(kind='release',evidence_kind='software_test',case_count=len(cases),
                      metrics=[Metric(name='pass_rate',value=passed/len(cases),samples=len(cases))],
                      gates=[Gate(name='Software tests',status='fail' if failed else 'not_run' if skipped else 'pass',
                                  criterion=f'All tests pass with no skipped cases. Passed {passed}, failed {failed}, skipped {skipped}.')],
                      limitations=['Software checks do not establish diagnosis accuracy, live AI answer quality, or production availability.',
                                   'Only this supplied test report is represented; other pipeline stages may not have run.'],**metadata)


def main():
    import argparse
    import os
    from pathlib import Path
    import requests
    parser=argparse.ArgumentParser(description='Prepare or publish customer-visible evaluation summaries')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('pilot','junit'):
        child=sub.add_parser(command,help='Prepare a summary from an existing report; run no evaluations')
        child.add_argument('--input',required=True)
        child.add_argument('--out',required=True)
        child.add_argument('--run-key',required=True)
        child.add_argument('--name',required=True)
        child.add_argument('--scope',required=True)
        child.add_argument('--revision')
        child.add_argument('--pipeline-url')
        child.add_argument('--completed-at',help='ISO timestamp with timezone; defaults to preparation time')
        if command=='pilot': child.add_argument('--method',required=True,choices=['baro','circa'])
    publish=sub.add_parser('publish',help='Publish a prepared summary with a separate pipeline token')
    publish.add_argument('--report',required=True)
    publish.add_argument('--url',required=True)
    args=parser.parse_args()
    try:
        if args.command=='publish':
            token=os.getenv('CORCA_EVALUATION_TOKEN','')
            endpoint=urlsplit(args.url)
            if (endpoint.scheme!='https' and not (endpoint.scheme=='http' and endpoint.hostname in ('localhost','127.0.0.1','::1'))) or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError('Use a reachable HTTPS CoRCA base address without embedded credentials or query parameters')
            if len(token)<16:
                raise ValueError('Set CORCA_EVALUATION_TOKEN in the pipeline secret environment')
            raw=Path(args.report).read_bytes()
            if len(raw)>100_000: raise ValueError('Summary exceeds 100 KB')
            report=Evaluation.model_validate_json(raw)
            response=requests.post(args.url.rstrip('/')+'/v1/evaluations',headers={'Authorization':'Bearer '+token},
                                   json=report.model_dump(mode='json'),timeout=30,allow_redirects=False)
            if response.status_code!=201: raise ValueError('CoRCA rejected publication; check access, schema, and run-key conflicts')
            print('Evaluation summary published.')
        else:
            raw=Path(args.input).read_bytes()
            if len(raw)>4*1024*1024: raise ValueError('Input report exceeds 4 MB')
            metadata={'run_key':args.run_key,'name':args.name,'scope':args.scope,'source_revision':args.revision,
                      'pipeline_url':args.pipeline_url,'completed_at':datetime.fromisoformat(args.completed_at.replace('Z','+00:00')) if args.completed_at else datetime.now(timezone.utc)}
            report=from_pilot(json.loads(raw),args.method,**metadata) if args.command=='pilot' else from_junit(Path(args.input),**metadata)
            with open(args.out,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as stream:
                stream.write(report.model_dump_json(indent=2)+'\n')
            print('Evaluation summary prepared; no evaluation or pipeline was executed.')
    except (ValueError,KeyError,TypeError,OSError,requests.RequestException):
        parser.exit(1,'Evaluation operation failed. Check inputs, required fields, output path, and deployment access. Existing evidence is never overwritten.\n')


if __name__=='__main__':
    main()

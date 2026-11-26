"""Turn reproducible research records into the console's validated case reports."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.quality import Evaluation, EvaluationCase, Metric
from corca.metrics import set_prf
from scripts.experiment import read_runs
from scripts.summarize import diagnosis_evidence, predicted_set


def case_report(cases, *, run_key, name, dataset_id, scope, evidence_kind, limitations):
    available = [c for c in cases if c.status=='no_significant_anomaly' or
                 c.status in ('diagnosed','partial_unexplained','partial_timeout') and c.predicted]
    available_ids = {c.case_id for c in available}
    faults = [c for c in cases if c.outcome=='fault']
    healthy = [c for c in available if c.outcome=='healthy']
    values = [('mean_f1',sum(set_prf(c.predicted,c.truth)[2] if c.case_id in available_ids else 0 for c in faults),len(faults)),
              ('complete_exact_match',sum(c.status=='diagnosed' and bool(c.predicted) and set(c.predicted)==set(c.truth) for c in faults),len(faults)),
              ('healthy_false_positive',sum(bool(c.predicted) for c in healthy),len(healthy)),
              ('unavailable_rate',len(cases)-len(available),len(cases))]
    return Evaluation(run_key=run_key,name=name,kind='diagnosis',completed_at=datetime.now(timezone.utc),
                      evidence_kind=evidence_kind,scope=scope,dataset_id=dataset_id,case_count=len(cases),cases=cases,
                      metrics=[Metric(name=n,value=float(v/d) if d else None,samples=d) for n,v,d in values],
                      limitations=limitations)


def reports(folder, dataset_id, label):
    folder = Path(folder)
    path = folder/'runs.jsonl'
    records = read_runs(path) if path.exists() else [json.loads(line) for line in (folder/'diagnoses.jsonl').read_text().splitlines()]
    calibration = json.loads((folder/'calibration.json').read_text())['methods']
    groups = {}
    for record in records:
        key = (record['method'],record.get('drop',0),record.get('seed',0),record.get('keep_resource',True),record.get('graph_mode','observed'))
        groups.setdefault(key,[]).append(record)
    for (method,drop,seed,keep,graph), group in sorted(groups.items()):
        if method not in calibration:
            raise ValueError(f'Missing calibration for {method}')
        main = drop==0 and keep and graph=='observed'
        # Every dropout seed remains in CSVs; one fixed seed keeps the UI concise.
        if drop and (drop!=.25 or seed!=0):
            continue
        variant = 'Primary' if main else '25% telemetry missing' if drop else 'Without resource preservation' if not keep else 'Static topology'
        for approach in (['Top-1','Top-2','Matched output size','CoRCA'] if main else ['CoRCA']):
            cases = []
            for rec in sorted(group,key=lambda r:r['case']):
                cal = calibration[method]
                tau = cal.get('fold_taus',{}).get(rec.get('case_id'),cal['tau'])
                roots = predicted_set(rec,tau)
                if approach=='CoRCA':
                    predicted = roots
                    status = diagnosis_evidence(rec,tau)['diagnosis_status']
                else:
                    count = len(roots) if approach=='Matched output size' else int(approach[-1])
                    predicted = rec.get('base_rank',[])[:count]
                    status = 'diagnosed' if predicted else 'failed'
                if status not in ('diagnosed','partial_unexplained','partial_timeout','insufficient_evidence'):
                    status = 'failed'
                relative = '/'.join(Path(rec['case']).parts[-2:])
                cases.append(EvaluationCase(case_id=hashlib.sha256(relative.encode()).hexdigest()[:24],
                    fault_type='+'.join(rec.get('faults',[])) or Path(rec['case']).parent.name,
                    outcome='fault',truth=rec['truth'],predicted=predicted,status=status,
                    seconds=rec.get('corca_seconds',rec.get('seconds')) if approach=='CoRCA' else rec.get('baseline_seconds')))
            identity = hashlib.sha256(json.dumps([dataset_id,method,drop,seed,keep,graph,approach]).encode()).hexdigest()[:24]
            yield case_report(cases,run_key='defense:'+identity,name=f'{label} · {method.upper()} · {approach} · {variant}',
                dataset_id=dataset_id,scope=label,evidence_kind='controlled_drill',limitations=[
                    'Controlled injected incidents; this report does not establish production accuracy.',
                    'Public thresholds are held out by repeat; local thresholds come from separate controls. Test labels do not set thresholds.',
                    'Missing, failed and partial results remain visible. Complete exact match requires a completed cause set.',
                    'Research runtime includes process startup and up to three rounds; it is not live early-stop response latency.',
                    'Matched output size gives the baseline the CoRCA output count; it isolates reranking, not an independently deployed policy.',
                    'Local dropout variants reuse the same incidents. They do not increase the independent sample size.'])

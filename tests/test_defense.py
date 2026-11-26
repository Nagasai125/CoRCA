import json
from pathlib import Path

import pytest

from backend.quality import EvaluationCase
from scripts.defense import copy_data, source_files
from scripts.report import matched_escalation
from scripts.review_reports import case_report, reports


def records():
    return [{'case':'a_cpu/1','case_id':'1','method':'baro','truth':['a'],
        'faults':['cpu'],'base_rank':['wrong','a','other'],'status':'ok',
        'rounds':[{'service':'wrong','residual':10},{'service':'a','residual':0}]},
        {'case':'b_cpu/2','case_id':'2','method':'baro','truth':['b'],
         'faults':['cpu'],'base_rank':[],'status':'error','rounds':[]}]


def test_matched_escalation_uses_output_count_and_keeps_failed_cases():
    result=matched_escalation('test',records(),{'baro':{'tau':5}})
    assert result['n_pred'].tolist()==[2,0]
    assert result['baseline_n_pred'].tolist()==[2,0]
    assert result['size_matched'].all()
    assert result['corca_F1'].mean()==pytest.approx(1/3)
    assert result['matched_F1'].mean()==pytest.approx(1/3)


def test_published_comparisons_keep_identical_cases_and_failures(tmp_path):
    (tmp_path/'runs.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records()))
    (tmp_path/'calibration.json').write_text(json.dumps({'methods':{'baro':{'tau':5,'fold_taus':{'1':20,'2':5}}}}))
    result=list(reports(tmp_path,'dataset-1','Test dataset'))
    assert len(result)==4
    assert all(r.case_count==2 and r.cases[1].status=='failed' for r in result)
    assert len({r.scope for r in result})==1
    assert len({tuple(c.case_id for c in r.cases) for r in result})==1
    assert result[-1].cases[0].predicted==['wrong']  # held-out fold, not the global tau
    assert result[-1].metrics[0].value==0


def test_case_report_handles_healthy_and_partial_results():
    cases=[EvaluationCase(case_id='h',fault_type='healthy',outcome='healthy',truth=[],predicted=[],status='no_significant_anomaly'),
           EvaluationCase(case_id='f',fault_type='cpu',outcome='fault',truth=['a'],predicted=['a'],status='partial_unexplained')]
    report=case_report(cases,run_key='test',name='Test',dataset_id='test',scope='test',evidence_kind='synthetic',limitations=['Fixture only'])
    metrics={m.name:m.value for m in report.metrics}
    assert metrics=={'mean_f1':1.,'complete_exact_match':0.,'healthy_false_positive':0.,'unavailable_rate':0.}


def test_review_inputs_exclude_raw_logs_and_incomplete_collections(tmp_path):
    source=tmp_path/'source';folder=source/'a_cpu/1';folder.mkdir(parents=True)
    (folder/'metrics.csv').write_text('time,a_cpu\n1,1\n2,10\n')
    (folder/'inject_time.txt').write_text('2')
    (folder/'collection.json').write_text('{"status":"complete"}')
    (folder/'raw-traces.jsonl').write_text('PRIVATE')
    copy_data(source,tmp_path/'copy',complete=True)
    assert not list((tmp_path/'copy').rglob('raw-traces.jsonl'))
    (folder/'collection.json').write_text('{"status":"incomplete"}')
    with pytest.raises(ValueError,match='Incomplete'):
        copy_data(source,tmp_path/'rejected',complete=True)


def test_source_snapshot_excludes_private_state_and_build_outputs():
    paths=list(source_files())
    assert paths and any(p.name=='Dockerfile' for p in paths)
    assert all(not any(part in ('.state','__pycache__','results','.venv') for part in p.parts) for p in paths)

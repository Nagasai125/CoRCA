"""Build and serve an isolated, reproducible committee-review workspace."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile

import requests
import uvicorn

from backend.api import create_app
from backend.quality import EvaluationCase
from corca.data import list_cases
from scripts.experiment import digest_file
from scripts.review_reports import case_report, reports

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('backend','corca','frontend','scripts','e2e','tests','lab','docs','.github')
SOURCE_FILES = ('pyproject.toml','uv.lock','requirements.txt','README.md','LICENSE','CONTRIBUTING.md','CHANGELOG.md','compose.yaml','.gitignore','.dockerignore')
DATA_FILES = ('data.csv','metrics.csv','inject_time.txt','ground_truth.json','topology.json','collection.json','telemetry-quality.json')


def write_json(path, value):
    path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def source_files():
    included = None
    if (ROOT/'.git').exists():
        git = subprocess.run(['git','-C',str(ROOT),'ls-files','--cached','--others','--exclude-standard'],capture_output=True,text=True)
        included = set(git.stdout.splitlines()) if git.returncode==0 else None
    for directory in SOURCE_DIRS:
        for path in (ROOT/directory).rglob('*'):
            relative = path.relative_to(ROOT/directory)
            if path.is_file() and (included is None or str(path.relative_to(ROOT)) in included) and not any(p.startswith('.') or p in ('__pycache__','shots') for p in relative.parts) and path.suffix in ('.py','.js','.cjs','.html','.css','.json','.md','.yaml','.yml','.toml','.lock','.txt','.woff','.png','.sh'):
                if directory=='e2e' and path.name in ('incident.txt',):
                    continue
                yield path
    for name in SOURCE_FILES:
        if (ROOT/name).is_file():
            yield ROOT/name
    yield ROOT/'backend/Dockerfile'


@contextmanager
def local_app(db):
    sock = socket.socket()
    sock.bind(('127.0.0.1',0))
    address = 'http://127.0.0.1:'+str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(create_app(db_path=db),log_level='warning'))
    thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+30
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.05)
        if not server.started:
            raise RuntimeError('Isolated review server did not start')
        yield address
    finally:
        server.should_exit=True
        thread.join(timeout=75)
        sock.close()
        if thread.is_alive():
            raise RuntimeError('Review server did not stop cleanly')


def request(address, method, path, **kwargs):
    response=requests.request(method,address+path,timeout=120,allow_redirects=False,**kwargs)
    if not response.ok:
        raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:300]}')
    return response.json()


def import_case(address, body, truth, kind, evidence, output):
    job=request(address,'POST','/v1/diagnoses/import',json=body)
    deadline=time.monotonic()+100
    while job['status'] in ('queued','running') and time.monotonic()<deadline:
        time.sleep(.2)
        job=request(address,'GET','/v1/diagnoses/'+job['id'])
    if job['status']!='done':
        raise RuntimeError('Imported review case did not complete: '+str(job))
    request(address,'POST','/v1/diagnoses/'+job['id']+'/review',json={
        'revision':0,'outcome':'fault' if truth else 'healthy','root_causes':truth,'evidence_kind':kind,'evidence':evidence})
    bundle={'diagnosis':job,**{name:request(address,'GET','/v1/diagnoses/'+job['id']+'/'+name) for name in ('evidence','review','uncertainty')}}
    bundle['review']=bundle['review']['review']
    write_json(output,bundle)
    return job


def publish(address, report, output):
    write_json(output,report.model_dump(mode='json'))
    request(address,'POST','/v1/evaluations',json=report.model_dump(mode='json'))


def copy_data(source, destination, complete=False):
    cases=list_cases(str(source))
    folders=[]
    for case in cases:
        folder=Path(case.path)
        if complete and (not (folder/'collection.json').is_file() or json.loads((folder/'collection.json').read_text()).get('status')!='complete'):
            raise ValueError(f'Incomplete local collection: {folder}')
        target=destination/folder.relative_to(source)
        target.mkdir(parents=True)
        for name in DATA_FILES:
            if (folder/name).is_file():
                shutil.copy2(folder/name,target/name)
        folders.append(target)
    return folders


def stage(out, manifest, name, module, *arguments):
    command=[sys.executable,'-m',module,*map(str,arguments)]
    started=time.monotonic()
    print(name+'…',flush=True)
    with (out/(name+'.log')).open('w') as log:
        result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                              env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'})
    manifest['stages'].append({'name':name,'command':command[1:],'seconds':time.monotonic()-started,'exit_code':result.returncode})
    write_json(out/'pipeline.json',manifest)
    if result.returncode:
        raise RuntimeError(f'{name} failed; inspect {out/(name+".log")}')


def evaluate(args,out,manifest):
    for name,path in [('public',args.public_data),('controls',args.controls),('heldout',args.heldout)]:
        if path is None:
            raise ValueError('evaluate needs --public-data, --controls and --heldout')
        copy_data(path.resolve(),out/'data'/name,complete=name!='public')
    controls=list_cases(str(out/'data/controls'))
    heldout=list_cases(str(out/'data/heldout'))
    if any(len(c.root_causes)!=1 for c in controls) or not any(len(c.root_causes)>1 for c in heldout):
        raise ValueError('Use single-fault controls and a held-out set containing concurrent faults')
    controls_hash={digest_file(Path(c.path)/'metrics.csv') for c in controls}
    if controls_hash & {digest_file(Path(c.path)/'metrics.csv') for c in heldout}:
        raise ValueError('Control and held-out incident data overlap')
    stage(out,manifest,'controls','lab.evaluate','--data',out/'data/controls','--out',out/'controls',
          '--rates','0','--seeds','0','--workers',args.workers,'--timeout','60')
    stage(out,manifest,'public','scripts.run_eval','--data',out/'data/public','--out',out/'public',
          '--methods','baro','circa','--workers',args.workers)
    stage(out,manifest,'public-summary','scripts.summarize',out/'public/runs.jsonl')
    stage(out,manifest,'heldout','lab.evaluate','--data',out/'data/heldout','--out',out/'heldout',
          '--calibration',out/'controls/calibration.json','--workers',args.workers,'--timeout','60')
    stage(out,manifest,'tables','scripts.report',out/'public',out/'heldout','--out',out/'tables')
    for directory in ('public','heldout'):
        report=json.loads((out/directory/'report.json').read_text())
        missing=sum(v['missing'] for v in report['coverage'].values()) if 'coverage' in report else report['missing']
        if missing:
            raise RuntimeError(f'{directory} has missing evaluation attempts')
    with local_app(out/'workspace.db') as address:
        count=len(list_cases(str(out/'data/public')))
        for directory,label in [('heldout','Local concurrent-fault recordings'),('public',f'RE1-OB: {count} single-fault incidents')]:
            files=sorted((out/'data'/directory).rglob('*.csv'))
            fingerprint=hashlib.sha256(''.join(digest_file(p) for p in files).encode()).hexdigest()[:24]
            for report in reports(out/directory,fingerprint,label):
                publish(address,report,out/'reports'/(report.run_key.split(':')[-1]+'.json'))
        from lab.client import incident_payload
        calibration=json.loads((out/'controls/calibration.json').read_text())
        for method in ('baro','circa'):
            payload=incident_payload(Path(heldout[0].path),method=method)
            payload['tau']=calibration['methods'][method]['tau']
            body={'environment':'defense-local-recording','incident':payload}
            import_case(address,body,heldout[0].root_causes,'controlled_drill',
                'Previously collected controlled fault injection; see held-out collection and ground-truth artifacts.',out/'exports'/('local-'+method+'.json'))
    manifest['evidence']='public_single_fault_and_local_controlled_concurrent_incidents'
    manifest['counts']={'public_incidents':len(list_cases(str(out/'data/public'))),'local_controls':len(controls),'local_heldout_incidents':len(heldout)}


def demo(out,manifest):
    from e2e.joint_fixture import fixture
    cases=[]
    with local_app(out/'workspace.db') as address:
        for healthy in (True,False):
            data=fixture(healthy=healthy)
            name='healthy' if healthy else 'concurrent'
            truth=[] if healthy else ['frontend','database']
            body={'environment':'synthetic-defense-demo','incident':{**data['metrics'],'tau':3},'traces':data['traces'],'logs':data['logs']}
            write_json(out/'inputs'/(name+'.json'),body)
            job=import_case(address,body,truth,'synthetic','Known simulated signals generated by the checked-in browser fixture; not a real incident.',out/'exports'/(name+'.json'))
            result=job['result']
            cases.append(EvaluationCase(case_id=name,fault_type='healthy' if healthy else 'two simulated faults',
                outcome='healthy' if healthy else 'fault',truth=truth,predicted=result.get('root_causes',[]),
                status=result['status'],seconds=result['processing_seconds'],diagnosis_id=job['id']))
        report=case_report(cases,run_key='defense:synthetic-workflow',name='CoRCA · synthetic workflow check',
            dataset_id='synthetic-joint-v1',scope='Two simulated windows through the actual import API and worker',
            evidence_kind='synthetic',limitations=['Synthetic data verifies software behavior, not research accuracy.',
                'Sensitivity is fixed at 3 for this demonstration; it is not calibrated for real telemetry.',
                'Candidate probabilities require qualifying independent confirmed incidents and are withheld here.'])
        publish(address,report,out/'reports/demo.json')
        if cases[0].status!='no_significant_anomaly' or set(cases[1].predicted)!=set(cases[1].truth):
            raise RuntimeError('Synthetic workflow regression; inspect the preserved exports')
    manifest['evidence']='synthetic_software_demonstration_only'
    manifest['counts']={'synthetic_windows':len(cases)}


def finish(out,manifest):
    manifest['status']='complete'
    manifest['completed_at']=datetime.now(timezone.utc).isoformat()
    write_json(out/'pipeline.json',manifest)
    (out/'REVIEW.md').write_text('''# CoRCA committee review

This workspace contains recorded evidence. Start the application from the project
root with `uv run --no-sync python -m scripts.defense serve --out review` after
extracting the review package. Open http://localhost:8048/app?view=quality.
Use Evaluations to select a run and compare baselines, then inspect individual
cases. Diagnoses contains recorded examples with evidence downloads.

`pipeline.json` lists executed stages. `checksums.json` fingerprints all artifacts.
`source-snapshot.zip` contains the source used. `tables/` contains main results,
paired differences, sensitivity, and matched-output-size comparisons when a full
evaluation was run. `heldout/summary.csv` contains ablations and telemetry loss.
Failures remain in the denominators. Dropout seeds are not independent incidents.

Run `uv run --no-sync python -m scripts.defense verify --out review` before review.
Install dependencies first using the root CONTRIBUTING.md. To reproduce a full
evaluation on the included inputs, run:

```
uv run --no-sync python -m scripts.defense evaluate --public-data review/data/public --controls review/data/controls --heldout review/data/heldout --out results/new-review --workers 4
```

A demo package contains synthetic inputs only. It proves the workflow runs,
not that its accuracy generalizes. A full package uses public single-fault data
and the supplied local recordings; inspect independent incident counts before
making a multi-fault accuracy claim. Repair estimates remain model-dependent;
candidate probabilities are withheld when independent calibration is inadequate.
The submission paper and historical result archives are not modified.
''')
    with zipfile.ZipFile(out/'source-snapshot.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in source_files():
            archive.write(path,path.relative_to(ROOT))
    hashes={str(p.relative_to(out)):digest_file(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='checksums.json'}
    write_json(out/'checksums.json',hashes)
    with zipfile.ZipFile(out/'committee-review.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in source_files():
            archive.write(path,Path('CoRCA')/path.relative_to(ROOT))
        for path in sorted(out.rglob('*')):
            if path.is_file() and path.name!='committee-review.zip':
                archive.write(path,Path('CoRCA/review')/path.relative_to(out))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['demo','evaluate','serve','verify'])
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--public-data',type=Path)
    parser.add_argument('--controls',type=Path)
    parser.add_argument('--heldout',type=Path)
    parser.add_argument('--workers',type=int,choices=range(1,5),default=2)
    parser.add_argument('--port',type=int,default=8048)
    args=parser.parse_args()
    out=args.out.resolve()
    if args.command=='serve':
        if not (out/'workspace.db').is_file():
            parser.error('No review workspace; run demo or evaluate first')
        uvicorn.run(create_app(db_path=out/'workspace.db'),host='127.0.0.1',port=args.port)
        return
    if args.command=='verify':
        hashes=json.loads((out/'checksums.json').read_text())
        if not isinstance(hashes,dict) or any(not isinstance(name,str) or not (out/name).resolve().is_relative_to(out) for name in hashes):
            parser.error('Invalid artifact manifest paths')
        mismatches=[name for name,digest in hashes.items() if not (out/name).is_file() or digest_file(out/name)!=digest]
        if mismatches:
            parser.exit(1,'Changed or missing artifacts: '+', '.join(mismatches)+'\n')
        print(f'Verified {len(hashes)} recorded artifacts. Serving or reviewing changes workspace.db; retain the original package.')
        return
    if out.exists():
        parser.error('Use a new output directory; existing evidence is never overwritten')
    out.mkdir(parents=True)
    for directory in ('reports','inputs','exports'):
        (out/directory).mkdir()
    manifest={'schema_version':1,'status':'running','command':args.command,'started_at':datetime.now(timezone.utc).isoformat(),
              'source_sha256':{str(p.relative_to(ROOT)):digest_file(p) for p in source_files()},'stages':[]}
    write_json(out/'pipeline.json',manifest)
    try:
        if args.command=='demo':
            demo(out,manifest)
        else:
            evaluate(args,out,manifest)
        if manifest['source_sha256']!={str(p.relative_to(ROOT)):digest_file(p) for p in source_files()}:
            raise RuntimeError('Source changed during this run; preserve these artifacts and rerun in a new folder')
        finish(out,manifest)
    except Exception as exc:
        manifest.update(status='failed',error=str(exc))
        write_json(out/'pipeline.json',manifest)
        parser.exit(1,str(exc)+'\n')
    print(f'Complete: {out}\nReview: {sys.executable} -m scripts.defense serve --out {out}',flush=True)


if __name__=='__main__':
    main()

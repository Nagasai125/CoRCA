"""Exercise telemetry import against a disposable local workspace (saves synthetic jobs)."""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect


from joint_fixture import fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Disposable local test server')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width':1440, 'height':1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url.rstrip('/')+'/app?view=diagnoses')
        page.click('#import-open')
        page.click('#import-submit')
        expect(page.locator('#import-error')).to_contain_text('Name the environment')
        page.fill('#import-environment', 'synthetic-browser-check')
        for name, value in fixture().items():
            page.set_input_files('#import-'+name, {'name':name+'.json', 'mimeType':'application/json', 'buffer':json.dumps(value).encode()})
        page.fill('#import-tau', '3')
        with page.expect_response(lambda r: '/v1/diagnoses/import' in r.url and r.request.method=='POST') as response:
            page.click('#import-submit')
        assert response.value.status==202, response.value.text()
        identifier = response.value.json()['id']
        def completed(job_id):
            deadline = time.monotonic()+45
            while time.monotonic()<deadline:
                job = page.request.get(args.url+'/v1/diagnoses/'+job_id).json()
                if job['status'] in ('done','failed'):
                    assert job['status']=='done', job
                    return job
                page.wait_for_timeout(300)
            raise AssertionError('Diagnosis did not complete')
        job = completed(identifier)
        assert job['result']['root_causes'], job
        assert job['result']['structural_model']['available'], job['result']['structural_model']
        page.reload()
        for title in ('Combined telemetry', 'Candidate reliability', 'Estimated effect of repair'):
            expect(page.get_by_role('heading', name=title, exact=True)).to_be_visible()
        expect(page.locator('.ref-signal-row')).to_have_count(5)
        page.screenshot(path=str(args.output/'joint-desktop.png'), full_page=True)
        page.locator('#detail .ref-technical summary').click()
        with page.expect_download() as downloaded:
            page.get_by_role('button', name='Download evidence', exact=True).click()
        payload = json.loads(Path(downloaded.value.path()).read_text())
        assert payload['uncertainty']['available'] is False
        assert {s['modality'] for s in payload['evidence']['series']}=={'metrics','traces','logs'}
        assert 'SYNTHETIC PRIVATE BODY' not in json.dumps(payload)
        with page.expect_response(lambda r: r.url.endswith('/rerun') and r.request.method=='POST') as repeated:
            page.get_by_role('button', name='Re-run diagnosis', exact=True).click()
        assert repeated.value.status==202
        rerun = completed(repeated.value.json()['id'])
        assert rerun['result']['root_causes']==job['result']['root_causes']
        assert rerun['result']['fusion']==job['result']['fusion']
        page.set_viewport_size({'width':375, 'height':950})
        page.reload()
        expect(page.get_by_role('heading', name='Combined telemetry', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(args.output/'joint-mobile.png'), full_page=True)
        page.goto(args.url.rstrip('/')+'/app?view=diagnoses')
        page.click('#import-open')
        expect(page.locator('#import-submit')).to_be_visible()
        assert page.locator('#import-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
        streams = fixture()
        bundle = {'incident': streams['metrics'], 'logs': streams['logs'], 'traces': streams['traces']}
        page.fill('#import-environment', 'wrapped-browser-check')
        page.set_input_files('#import-metrics', {'name':'bundle.json', 'mimeType':'application/json', 'buffer':json.dumps(bundle).encode()})
        with page.expect_response(lambda r: r.url.endswith('/v1/diagnoses/import') and r.request.method=='POST') as imported:
            page.click('#import-submit')
        assert imported.value.status == 202, imported.value.text()
        wrapped = completed(imported.value.json()['id'])
        path = '/v1/diagnoses/' + wrapped['id']
        evidence = page.request.get(args.url + path + '/evidence').json()
        assert {s['modality'] for s in evidence['series']} == {'metrics', 'logs', 'traces'}
        for status in ('failed', 'cancelled'):
            # Present a result-less history row without racing the worker; the rerun API is real.
            page.route('**/v1/diagnoses?limit=200', lambda route: route.fulfill(json=[dict(wrapped, status=status, result=None)]))
            page.goto(args.url + '/app?view=diagnoses#' + wrapped['id'])
            page.reload()
            page.locator('#detail .ref-technical summary').click()
            with page.expect_response(lambda r: r.url.endswith(path+'/rerun') and r.request.method=='POST') as retry:
                page.get_by_role('button', name='Re-run diagnosis', exact=True).click()
            assert retry.value.status == 202, retry.value.text()
            page.unroute('**/v1/diagnoses?limit=200')
            assert completed(retry.value.json()['id'])['result']['fusion'] == wrapped['result']['fusion']
        assert not errors, errors
        browser.close()
    print('PASS: real import, evidence, repair estimate, calibration abstention, download, immutable rerun, mobile layout; no JavaScript errors.')


if __name__=='__main__':
    main()

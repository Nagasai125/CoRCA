"""Check a prepared review workspace through the real browser and API."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8048')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.url+'/app?view=quality')
        run=page.get_by_label('Evaluation run',exact=True)
        expect(run).to_be_visible()
        options=run.locator('option').evaluate_all('(items)=>items.map(e=>({value:e.value,label:e.textContent}))')
        primary=next((o for o in options if 'BARO · CoRCA · Primary' in o['label']),options[0])
        run.select_option(primary['value'])
        expect(page.locator('.ref-set-metrics .eval-kpi')).to_have_count(4)
        expect(page.locator('.ref-set-metrics')).to_contain_text('Cause-set F1')
        if 'Primary' in primary['label']:
            comparison=page.get_by_label('Compare evaluation with')
            peers=comparison.locator('option').evaluate_all('(items)=>items.map(e=>({value:e.value,label:e.textContent}))')
            baseline=next(o for o in peers if 'BARO · Top-1 · Primary' in o['label'])
            comparison.select_option(baseline['value'])
            expect(comparison).to_have_value(baseline['value'])
        page.screenshot(path=str(args.output/'evaluations-desktop.png'),full_page=True)
        page.get_by_role('button',name='Pipeline tools',exact=True).click()
        expect(page.locator('#quality-upload-form')).to_be_visible()
        jobs=page.request.get(args.url+'/v1/diagnoses?limit=200').json()
        job=next(j for j in jobs if j.get('result',{}).get('root_causes'))
        page.goto(args.url+'/app?view=diagnoses#'+job['id'])
        expect(page.locator('.ref-candidates tbody tr')).to_have_count(len(job['result']['root_causes']))
        page.locator('#detail .ref-technical summary').click()
        with page.expect_download() as download:
            page.get_by_role('button',name='Download evidence',exact=True).click()
        saved=json.loads(Path(download.value.path()).read_text())
        assert saved['diagnosis']['id']==job['id'] and saved['evidence']['available']
        page.screenshot(path=str(args.output/'diagnosis-desktop.png'),full_page=True)
        page.set_viewport_size({'width':375,'height':950})
        page.goto(args.url+'/app?view=quality')
        expect(page.get_by_label('Evaluation run',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(args.output/'evaluations-mobile.png'),full_page=True)
        assert not errors,errors
        browser.close()
    print('PASS: published evaluation, baseline comparison when present, pipeline tools, diagnosis evidence download, mobile layout.')


if __name__=='__main__':
    main()

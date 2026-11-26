"""Read-only browser regressions using controlled API responses.

Requires Playwright and a running development console. No workspace data is saved.
"""
import argparse
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.logs import analyze_logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--chromium', help='Optional Chromium executable path')
    args = parser.parse_args()
    base = args.url.rstrip('/')
    job = {'id': 'browser-regression-job', 'status': 'done', 'inject_time': 1793832315,
           'window': [1793832015, 1793832435], 'method': 'baro', 'attempts': 1,
           'result': {'status': 'diagnosed', 'root_causes': ['cartservice'],
                      'rounds': [{'service': 'cartservice', 'score': 12.4, 'explained': []}],
                      'threshold_mode': 'manual', 'tau': 6}}
    jobs = [job]
    names = ['cartservice', 'database', 'frontend']
    inventory = {'revision': 'browser-check', 'selected_services': names,
                 'services': [{'name': name, 'metric_kinds': ['cpu'], 'coverage': {'cpu': {'coverage': 1}},
                               'missing_kinds': [], 'status': 'reporting'} for name in names],
                 'dependencies': [['frontend', 'cartservice'], ['cartservice', 'database']],
                 'topology_configured': True, 'metric_kinds': ['cpu'], 'resource_only': True,
                 'checked_at': 1793832315}
    log = (b'2026-11-04T18:00:00Z WARN service=cartservice retry\n'
           b'2026-11-04T18:01:00Z ERROR service=cartservice timeout\n'
           b'2026-11-04T18:01:01Z ERROR service=cartservice timeout')
    report = analyze_logs(log, 'browser-check.log')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({'executable_path': args.chromium} if args.chromium else {}))
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
        metadata = page.request.get(base + '/v1/integrations').json()
        metadata.update(provider='prometheus', revision='browser-check', thresholds={'baro': 6})
        metadata['prometheus'].update(configured=True, topology='custom', service_count=3,
                                      step_seconds=5, metric_kinds=['cpu'])
        page.route('**/v1/config', lambda route: route.fulfill(json={'prometheus': True, 'methods': ['baro'], 'alert_method': 'baro'}))
        page.route('**/v1/integrations', lambda route: route.fulfill(json=metadata))
        page.route('**/v1/services', lambda route: route.fulfill(json=inventory))
        page.route('**/v1/diagnoses?*', lambda route: route.fulfill(json=jobs))
        page.route('**/v1/diagnoses/browser-regression-job/evidence', lambda route: route.fulfill(json={
            'available':False,'series':[],'edges':inventory['dependencies'],'scope':names,
            'alerts':[],'note':'Controlled legacy diagnosis without captured metric samples.'}))
        page.route('**/v1/diagnoses/browser-regression-job/review', lambda route: route.fulfill(json={'review':None}))
        page.route('**/v1/help/ask', lambda route: route.fulfill(json={'answer': 'Controlled local answer', 'mode': 'guide', 'notice': 'Guide', 'sources': []}))
        page.route('**/v1/logs/analyze?*', lambda route: route.fulfill(json=report))

        page.goto(base + '/app?view=diagnoses')
        expect(page.locator('#new-form')).to_be_visible()
        expect(page.locator('#new-btn')).to_have_text('Hide form')
        page.fill('#started', '2026-11-04T12:00')
        page.locator('.help-launch').click()
        page.locator('#help-suggestions button').first.click()
        expect(page.locator('#started')).to_have_value('2026-11-04T12:00')
        page.keyboard.press('Escape')
        page.select_option('#job-status', 'failed')
        page.fill('#workspace-search', job['id'])
        page.press('#workspace-search', 'Enter')
        expect(page.locator('#job-status')).to_have_value('all')
        expect(page.locator('#list')).to_contain_text(job['id'][:8])

        page.fill('#job-search', '')
        page.goto(base + '/app?view=diagnoses#' + job['id'])
        expect(page.locator('.ref-candidates tbody tr')).to_have_count(1)
        page.locator('#detail .ref-technical summary').click()
        page.locator('#detail .ref-technical summary').focus()
        jobs.insert(0, {**job, 'id': 'unrelated-new-job'})
        page.evaluate('document.querySelector("#refresh-btn").click()')
        expect(page.locator('#list')).to_contain_text('unrelate')
        assert page.locator('#detail .ref-technical').evaluate('(element)=>element.open')
        expect(page.locator('#detail .ref-technical summary')).to_be_focused()
        page.get_by_role('button', name='Diagnoses', exact=True).click()
        page.fill('#job-search', job['id'])
        page.locator('#list button').first.click()
        expect(page.locator('.ref-candidates tbody tr')).to_have_count(1)
        page.get_by_role('button', name='Service map', exact=True).click()
        expect(page.locator('.ref-inspector')).to_be_visible()
        page.locator('.ref-inspector').get_by_role('button', name='Follow', exact=True).click()
        expect(page.locator('.ref-inspector h2')).to_have_text('cartservice')

        page.locator('.sidebar [data-view=integrations]').click()
        page.locator('.setup-tabs [data-view=services]').click()
        page.locator('.service-map-disclosure > summary').click()
        page.select_option('#map-service', 'cartservice')
        expect(page.locator('#map-graph .map-node')).to_have_count(2)
        page.get_by_role('button', name='database', exact=True).click()
        expect(page.locator('.map-focus')).to_contain_text('1 callers · 0 callees')
        expect(page.locator('.map-focus')).to_be_focused()
        page.click('#map-show-service')
        expect(page.locator('#service-search')).to_have_value('database')
        expect(page.locator('#service-inventory tbody tr')).to_have_count(1)

        page.locator('.sidebar [data-view=quality]').click()
        page.locator('.help-launch').click()
        expect(page.locator('#help-context')).to_contain_text('Context: Evaluations')
        expect(page.locator('#help-question')).to_be_focused()
        page.keyboard.press('Escape')
        page.locator('.sidebar [data-view=overview]').click()
        page.get_by_role('button', name='Analyze a log file').press('Enter')
        expect(page.locator('#logs-title')).to_be_focused()
        page.set_input_files('#logs-file', {'name': 'browser-check.log', 'mimeType': 'text/plain', 'buffer': log})
        page.click('#logs-analyze')
        expect(page.locator('.log-bucket')).to_have_count(2)
        page.locator('.log-bucket').first.click()
        expect(page.locator('.chart-scrubber')).to_have_value('0')
        assert '0 errors, 1 warnings, 1 entries' in page.locator('.chart-scrubber').get_attribute('aria-valuetext')
        page.locator('.chart-scrubber').focus()
        page.keyboard.press('End')
        expect(page.locator('.log-bucket').last).to_have_attribute('aria-pressed', 'true')
        assert '2 errors, 0 warnings, 2 entries' in page.locator('.chart-scrubber').get_attribute('aria-valuetext')
        for width in (320, 375, 768):
            page.set_viewport_size({'width': width, 'height': 950})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto(base + '/app?view=services')
            expect(page.locator('#map-service')).to_be_enabled()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert all('/v1/help/ask' in url or '/v1/logs/analyze?' in url for url in posts), posts
        session = {'enabled': True, 'user': {'name': 'Browser tester', 'role': 'responder'}}
        page.route('**/v1/access/session', lambda route: route.fulfill(json=session))

        def logout(route):
            assert route.request.method == 'POST'
            session['user'] = None
            route.fulfill(json={})

        page.route('**/v1/access/logout', logout)
        page.set_viewport_size({'width': 320, 'height': 800})
        page.goto(base + '/app?view=overview')
        expect(page.locator('#sign-out')).to_be_in_viewport()
        page.get_by_role('button', name='Sign out').click()
        expect(page.locator('#account-login')).to_be_visible()
        assert not errors, errors
        browser.close()
    print('PASS: console regressions including Evaluations help, Home to logs focus, and mobile sign out; no JavaScript errors or configuration writes.')


if __name__ == '__main__':
    main()

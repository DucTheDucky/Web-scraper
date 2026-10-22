"""Offline parsing tests and opt-in integration tests against a disposable SQL database.

Run: python -B -m unittest discover -s tests -v
SQL tests: set ADY_RUN_SQL_TESTS=1 first (creates/drops ADY_Task11_Test_<UUID>).
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'main'))
import main as scraper
import sqlserver_store as storage

LIST_URL = scraper.DEFAULT_LIST_URL


def url(post_id):
    return f'https://batdongsan.com.vn/ban-nha-rieng-da-nang/test-pr{post_id}'


def detail(post_id='900000001'):
    return f'''<html><title>Property</title><div class="js__product-detail-web" prid="{post_id}">
      <h1 class="re__pr-title">Chính chủ bán nhà lô góc Đà Nẵng</h1>
      <div class="re__pr-short-info-item"><span class="title">Mức giá</span><span class="value">3 tỷ 950 triệu</span></div>
      <div class="re__pr-short-info-item"><span class="title">Diện tích</span><span class="value">100 m²</span></div>
      <div class="re__pr-short-info-item"><span class="title">Ngày đăng</span><span class="value">22/10/2026</span></div>
      <div class="re__pr-short-info-item"><span class="title">Ngày hết hạn</span><span class="value">22/11/2026</span></div>
      <div class="re__pr-short-info-item"><span class="title">Pháp lý</span><span class="value">Sổ đỏ</span></div>
      <div class="re__pr-short-info-item"><span class="title">Đường vào</span><span class="value">5,5 m</span></div>
      <div class="re__pr-short-info-item"><span class="title">Mặt tiền</span><span class="value">4 m</span></div>
      <div class="re__pr-short-info-item"><span class="title">Hướng nhà</span><span class="value">Đông Nam</span></div>
      <div class="re__pr-address">Phường Hải Châu, Quận Hải Châu, Đà Nẵng</div>
      <div class="re__pr-description">Không qua môi giới.</div>
    </div><div class="re__contact-name" title="Nguyễn Văn A"></div></html>'''


def row(post_id='900000001'):
    return dict(scraper.parse_detail_page(detail(post_id), url(post_id)),
                Scraped_At='2026-10-22T07:00:00+00:00', Source_List_URL=LIST_URL)


class ParserTests(unittest.TestCase):
    def test_plain_launch_defaults_to_verified_edge_mode(self):
        args = scraper.build_parser().parse_args([])
        self.assertEqual(args.fetcher, 'playwright')
        self.assertEqual(args.browser_channel, 'msedge')
        self.assertTrue(args.skip_analytics_bootstrap)

    def test_analytics_loading_is_explicit_and_old_skip_alias_still_works(self):
        self.assertFalse(scraper.build_parser().parse_args(['--load-analytics-bootstrap']).skip_analytics_bootstrap)
        self.assertTrue(scraper.build_parser().parse_args(['--experimental-skip-gtm-bootstrap']).skip_analytics_bootstrap)

    def test_vietnamese_source_and_english_classification(self):
        parsed = row()
        self.assertEqual(set(parsed), set(scraper.COLUMNS))
        self.assertEqual(parsed['Price_VND'], 3950000000)
        self.assertEqual(parsed['Price_Per_M2'], 39500000)
        self.assertEqual(parsed['Price_Per_M2_Source'], 'calculated')
        self.assertEqual(parsed['Seller_Type'], 'Owner (self-reported)')
        self.assertTrue(parsed['Is_Corner_Lot'])
        self.assertIsNone(parsed['Verified_Status'])
        self.assertEqual(parsed['Province'], 'Đà Nẵng')

    def test_duplicate_price_labels_preserve_total_and_exact_unit_price(self):
        html = detail().replace(
            '<span class="title">Mức giá</span><span class="value">3 tỷ 950 triệu</span>',
            '<span class="title">Khoảng giá</span><span class="value">3,5 tỷ</span>'
            '<span class="ext">~35 triệu/m²</span>')
        specification = ('<div class="re__pr-specs-content-item">'
                         '<span class="re__pr-specs-content-item-title">Khoảng giá</span>'
                         '<span class="re__pr-specs-content-item-value">35 triệu/m²</span></div>')
        html = html.replace('</h1>', '</h1>' + specification)
        for fixture in (html, html.replace(specification, '').replace('</div><div class="re__contact-name"',
                                                                     specification + '</div><div class="re__contact-name"')):
            with self.subTest(fixture=fixture):
                parsed = scraper.parse_detail_page(fixture, url('900000001'))
                self.assertEqual(parsed['Price_Text'], '3,5 tỷ')
                self.assertEqual(parsed['Price_VND'], 3500000000)
                self.assertEqual(parsed['Price_Per_M2'], 35000000)
                self.assertEqual(parsed['Price_Per_M2_Source'], 'displayed')

    def test_unit_price_only_does_not_require_total_price(self):
        parsed = scraper.parse_detail_page(detail().replace('3 tỷ 950 triệu', '35 triệu/m²'), url('900000001'))
        self.assertEqual(parsed['Price_Text'], '35 triệu/m²')
        self.assertIsNone(parsed['Price_VND'])
        self.assertEqual(parsed['Price_Per_M2'], 35000000)
        self.assertEqual(parsed['Price_Per_M2_Source'], 'displayed')

    def test_missing_price_keeps_values_unknown(self):
        parsed = scraper.parse_detail_page(detail().replace('3 tỷ 950 triệu', ''), url('900000001'))
        self.assertEqual(parsed['Price_Text'], '')
        self.assertIsNone(parsed['Price_VND'])
        self.assertIsNone(parsed['Price_Per_M2'])

    def test_prices_and_unknowns(self):
        for text in ('Thỏa thuận', 'khoảng 3 tỷ', '3.950 tỷ', '3-4 tỷ'):
            self.assertIsNone(scraper.to_vnd(text))
        self.assertEqual(scraper.to_vnd('3,95 tỷ'), 3950000000)
        self.assertIsNone(storage.parse_bool(None))
        self.assertEqual(storage.parse_bool(False), 0)

    def test_rental_period_is_english_without_altering_price_text(self):
        html = detail().replace('3 tỷ 950 triệu', '20 triệu/tháng')
        parsed = scraper.parse_detail_page(html, url('900000001'))
        self.assertEqual(parsed['Price_Period'], 'month')
        self.assertEqual(parsed['Price_Text'], '20 triệu/tháng')

    def test_pagination_and_scope(self):
        html = (f'<div id="product-lists-web"><a class="js__card" href="{url(1)}">One</a></div>'
                '<div class="re__pagination-group"><a href="/nha-dat-ban-da-nang/p2">2</a></div>')
        result = scraper.parse_list_page(html, LIST_URL)
        self.assertEqual(result.detail_urls, [url(1)])
        self.assertEqual(result.next_url, LIST_URL + '/p2')
        with self.assertRaises(ValueError):
            scraper.valid_list_url('https://example.com/list')

    def test_access_blocks_stop_instead_of_falling_back(self):
        with self.assertRaises(scraper.BlockedError):
            scraper._check_blocked(scraper.Response(403, LIST_URL, '', {}))
        with self.assertRaises(scraper.BlockedError):
            scraper._check_blocked(scraper.Response(200, LIST_URL, '<title>Verify you are human</title>', {}))

    def test_cloudflare_challenge_reports_cause_and_omits_query(self):
        response = scraper.Response(403, LIST_URL + '?session=private', '',
                                    {'cf-mitigated': 'challenge', 'cf-ray': 'example-SYD'})
        with self.assertRaises(scraper.BlockedError) as caught:
            scraper._check_blocked(response)
        message = str(caught.exception)
        self.assertIn('Cloudflare access challenge', message)
        self.assertIn('VPN/proxy', message)
        self.assertIn('example-SYD', message)
        self.assertNotIn('session=private', message)

    def test_cli_rejects_old_file_options_and_invalid_run_ids(self):
        import contextlib
        import io
        for args in (['--state', 'old.sqlite'], ['--output', 'old.csv'], ['--resume', 'bad'], ['--delay', '1']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                scraper.build_parser().parse_args(args)


class BrowserResponseTests(unittest.TestCase):
    def browser(self):
        browser = scraper.BrowserFetcher(lambda address: True)
        browser._active = True
        browser._expected = url('900000001')
        browser.log = lambda message: None
        browser.actions = []
        browser._finish_interception = lambda method, params: browser.actions.append((method, params))
        return browser

    @staticmethod
    def event(address, kind='XHR'):
        return {'requestId': 'fixture-response', 'request': {'url': address}, 'resourceType': kind,
                'responseStatusCode': 403,
                'responseHeaders': [{'name': 'cf-mitigated', 'value': 'challenge'},
                                    {'name': 'cf-ray', 'value': 'fixture-SIN'}]}

    def test_late_optional_telemetry_challenge_does_not_stop_property(self):
        browser = self.browser()
        browser._paused_response(self.event('https://batdongsan.com.vn/gtganalytics/ag/g/c?event=engagement'))
        self.assertIsNone(browser._error)
        self.assertEqual(browser.actions[0][0], 'Fetch.failRequest')

    def test_optional_bootstrap_is_skipped_at_request_boundary(self):
        browser = self.browser()
        self.assertTrue(browser._skip_optional_bootstrap('https://batdongsan.com.vn/gtganalytics/', 'script'))
        for address, kind in (
            ('https://batdongsan.com.vn/gtganalytics/', 'document'),
            ('https://batdongsan.com.vn/gtganalytics/required.js', 'script'),
            ('https://other.example/gtganalytics/', 'script'),
            ('https://batdongsan.com.vn/gtganalytics/?other=1', 'script'),
        ):
            self.assertFalse(browser._skip_optional_bootstrap(address, kind))

    def test_late_optional_bootstrap_challenge_is_discarded(self):
        browser = self.browser()
        browser._paused_response(self.event('https://batdongsan.com.vn/gtganalytics/', 'Script'))
        self.assertIsNone(browser._error)
        self.assertEqual(browser.actions[0][0], 'Fetch.failRequest')

    def test_explicitly_loaded_bootstrap_keeps_block_detection(self):
        browser = self.browser()
        browser.skip_analytics_bootstrap = False
        browser._paused_response(self.event('https://batdongsan.com.vn/gtganalytics/', 'Script'))
        self.assertIsInstance(browser._error, scraper.BlockedError)

    def test_bootstrap_as_main_document_is_not_exempt(self):
        browser = self.browser()
        browser._paused_response(self.event('https://batdongsan.com.vn/gtganalytics/', 'Document'))
        self.assertIsInstance(browser._error, scraper.BlockedError)

    def test_required_resources_still_stop_on_challenge(self):
        browser = self.browser()
        browser._paused_response(self.event('https://batdongsan.com.vn/js/required.js', 'Script'))
        self.assertIsInstance(browser._error, scraper.BlockedError)
        self.assertEqual(browser._error.diagnostic['classification'], 'required_resource_blocked')

    def test_main_document_still_stops_on_challenge(self):
        browser = self.browser()
        browser._paused_response(self.event(url('900000001'), 'Document'))
        self.assertIsInstance(browser._error, scraper.BlockedError)
        self.assertEqual(browser._error.diagnostic['classification'], 'main_blocked')

    def test_similar_telemetry_path_is_not_exempt(self):
        browser = self.browser()
        browser._paused_response(self.event('https://batdongsan.com.vn/gtganalytics/ag/g/required'))
        self.assertIsInstance(browser._error, scraper.BlockedError)


@unittest.skipUnless(os.environ.get('ADY_RUN_SQL_TESTS') == '1', 'set ADY_RUN_SQL_TESTS=1 for disposable SQL integration tests')
class SqlServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = os.environ.get('ADY_TEST_SERVER', storage.DEFAULT_SERVER)
        cls.database = 'ADY_Task11_Test_' + uuid.uuid4().hex
        cls.master = storage.connect_database(cls.server, 'master')
        cls.master.autocommit = True
        cls.master.cursor().execute(f'CREATE DATABASE [{cls.database}]')

    @classmethod
    def tearDownClass(cls):
        # Only drop the exact uniquely named database created by this suite.
        if not cls.database.startswith('ADY_Task11_Test_') or len(cls.database) != 48:
            raise RuntimeError('Unexpected test database name; refusing cleanup.')
        cls.master.cursor().execute(f'ALTER DATABASE [{cls.database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
        cls.master.cursor().execute(f'DROP DATABASE [{cls.database}]')
        cls.master.close()

    def setUp(self):
        self.stores = []
        self.store = self.new_store()

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()

    def new_store(self, **kwargs):
        kwargs.setdefault('list_url', LIST_URL)
        store = storage.SqlServerStore(server=self.server, database=self.database, **kwargs)
        self.stores.append(store)
        return store

    def queue(self, post_id=None):
        post_id = post_id or str(uuid.uuid4().int)[:12]
        self.store.record_list_page(LIST_URL, [url(post_id)], None)
        return self.store.next_pending(), row(post_id)

    def scalar(self, sql, *params):
        return next(iter(self.store._one(sql, *params).values()))

    def test_full_record_roundtrip_and_normalized_view(self):
        item, parsed = self.queue()
        parsed['Verified_Status'] = False
        self.assertTrue(self.store.save_listing(item, parsed))
        self.assertEqual(list(self.store.listings()), [parsed])
        saved = self.store._one('SELECT * FROM dbo.batdongsan_data WHERE Post_ID=?', parsed['Post_ID'])
        self.assertEqual(set(saved), set(scraper.COLUMNS))
        for key in ('Title', 'Price_Text', 'Price_VND', 'Price_Per_M2', 'Address', 'Province', 'District',
                    'Ward', 'Post_ID', 'Legal_Status', 'Direction', 'Seller_Type', 'Contact_Name', 'URL',
                    'Price_Per_M2_Source', 'Price_Period', 'Corner_Keywords', 'Seller_Keywords', 'Source_List_URL'):
            self.assertEqual(saved[key], parsed[key], key)
        self.assertEqual(str(saved['Post_Date']), '2026-10-22')
        self.assertEqual(str(saved['Expiration_Date']), '2026-11-22')
        self.assertEqual(str(saved['Area']), '100.0000')
        self.assertEqual(str(saved['Frontage']), '4.0000')
        self.assertEqual(str(saved['Road_Width']), '5.5000')
        self.assertEqual(saved['Scraped_At'].isoformat(timespec='seconds'), parsed['Scraped_At'])
        self.assertFalse(saved['Verified_Status'])
        self.assertTrue(saved['Is_Corner_Lot'])
        self.assertEqual(self.store.count('done'), 1)

    def test_same_run_replay_is_idempotent(self):
        item, parsed = self.queue()
        self.store.save_listing(item, parsed)
        self.assertFalse(self.store.save_listing(item, parsed))
        self.assertEqual(self.store.count('done'), 1)
        self.assertEqual(len(list(self.store.listings())), 1)

    def test_new_run_updates_property_but_keeps_independent_snapshot_and_limits(self):
        item, parsed = self.queue()
        self.store.save_listing(item, parsed)
        old_id = self.scalar('SELECT listing_id FROM dbo.listings WHERE post_id=?', parsed['Post_ID'])
        other = self.new_store()
        self.assertEqual(other.count('done'), 0)
        other.record_list_page(LIST_URL, [parsed['URL']], None)
        changed = dict(parsed, Price_VND=4000000000)
        self.assertTrue(other.save_listing(other.next_pending(), changed))
        self.assertEqual(self.scalar('SELECT listing_id FROM dbo.listings WHERE post_id=?', parsed['Post_ID']), old_id)
        self.assertEqual(list(self.store.listings())[0]['Price_VND'], 3950000000)
        self.assertEqual(list(other.listings())[0]['Price_VND'], 4000000000)

    def test_failure_after_property_write_rolls_back_and_can_retry(self):
        item, parsed = self.queue()
        real = storage.save_property
        def fail(cursor, row):
            real(cursor, row)
            raise RuntimeError('Simulated storage failure')
        with patch.object(storage, 'save_property', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.store.save_listing(item, parsed)
        self.assertEqual(self.scalar('SELECT COUNT(*) AS n FROM dbo.listings WHERE post_id=?', parsed['Post_ID']), 0)
        self.assertEqual(self.store.count('pending'), 1)
        self.assertEqual(list(self.store.listings()), [])
        self.assertTrue(self.store.save_listing(item, parsed))

    def test_failure_marking_done_rolls_back_snapshot_and_property(self):
        item, parsed = self.queue()
        with self.store._transaction() as cursor:
            cursor.execute("CREATE OR ALTER TRIGGER dbo.test_reject_done ON dbo.crawl_queue AFTER UPDATE AS "
                           "BEGIN IF EXISTS(SELECT 1 FROM inserted WHERE status='done') "
                           "THROW 51000, 'Simulated queue commit failure', 1; END")
        try:
            with self.assertRaises(storage.pyodbc.Error):
                self.store.save_listing(item, parsed)
            self.assertEqual(self.store.count('pending'), 1)
            self.assertEqual(list(self.store.listings()), [])
            self.assertEqual(self.scalar('SELECT COUNT(*) AS n FROM dbo.listings WHERE post_id=?', parsed['Post_ID']), 0)
        finally:
            with self.store._transaction() as cursor:
                cursor.execute('DROP TRIGGER dbo.test_reject_done')

    def test_keyboard_interrupt_rolls_back(self):
        item, parsed = self.queue()
        real = storage.save_property
        def interrupt(cursor, row):
            real(cursor, row)
            raise KeyboardInterrupt
        with patch.object(storage, 'save_property', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save_listing(item, parsed)
        self.assertEqual(self.store.count('pending'), 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) AS n FROM dbo.listings WHERE post_id=?', parsed['Post_ID']), 0)

    def test_identity_mismatch_is_failed_and_bounded_retries(self):
        item, parsed = self.queue()
        bad = dict(parsed, Post_ID='999')
        for attempt in range(3):
            self.assertFalse(self.store.save_listing(item, bad))
            self.assertEqual(self.store.issues()[0]['attempts'], attempt + 1)
            self.assertEqual(self.store.reset_failed(), 1 if attempt < 2 else 0)
        self.assertEqual(self.store.count('failed'), 1)
        self.assertEqual(list(self.store.listings()), [])

    def test_list_page_failure_rolls_back_discovered_links(self):
        with self.assertRaises(ValueError):
            self.store.record_list_page(LIST_URL, [url(12345), 'https://example.com/bad'], LIST_URL + '/p2')
        self.assertEqual(self.store.total_found(), 0)
        self.assertEqual(self.store.pages_done(), 0)
        self.assertEqual(self.store.next_list_page()['url'], LIST_URL)

    def test_repeated_pages_and_replayed_commits(self):
        links = [url(101), url(102)]
        self.assertEqual(self.store.record_list_page(LIST_URL, links, LIST_URL + '/p2'), (2, False))
        self.assertEqual(self.store.record_list_page(LIST_URL, links, LIST_URL + '/p2'), (0, True))
        self.assertEqual(self.store.record_list_page(LIST_URL + '/p2', links, LIST_URL + '/p3'), (0, True))
        self.assertIsNone(self.store.next_list_page())
        self.assertEqual(self.store.total_found(), 2)

    def test_exclusive_run_lock_and_release(self):
        with self.assertRaises(RuntimeError):
            storage.SqlServerStore(server=self.server, database=self.database, run_id=self.store.run_id)
        run_id = self.store.run_id
        self.store.close()
        self.stores.remove(self.store)
        resumed = self.new_store(run_id=run_id)
        self.assertEqual(resumed.run_id, run_id)

    def test_resume_in_fresh_process_without_local_dataset_files(self):
        item, parsed = self.queue()
        self.store.save_listing(item, parsed)
        run_id = self.store.run_id
        self.store.close()
        self.stores.remove(self.store)
        script = Path(scraper.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', str(script), '--server', self.server,
                                     '--database', self.database, '--resume', run_id, '--max-items', '1'],
                                    cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Reached the property limit.', result.stdout)
            self.assertIn(str(script), result.stdout)
            self.assertIn('Resume in PowerShell from any folder:', result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_run_and_wrong_url_are_rejected(self):
        with self.assertRaises(ValueError):
            storage.SqlServerStore(server=self.server, database=self.database, run_id=str(uuid.uuid4()))
        self.store.close()
        self.stores.remove(self.store)
        with self.assertRaises(ValueError):
            storage.SqlServerStore(server=self.server, database=self.database, run_id=self.store.run_id,
                                   list_url='https://batdongsan.com.vn/nha-dat-ban-ha-noi')

    def test_multiple_properties_one_run_and_contact_omission(self):
        class FixtureCrawler:
            def fetch_parse(inner, address, kind, parser):
                if kind == 'list':
                    html = '<div id="product-lists-web">' + ''.join(
                        f'<a class="js__card" href="{url(n)}">Property</a>' for n in (201, 202, 203)) + '</div>'
                else:
                    post_id = scraper.PRODUCT_PATH_RE.search(address)[1]
                    html = detail(post_id)
                return parser(scraper.Response(200, address, html, {}))
        reason, stats = scraper.run(self.store, FixtureCrawler(), max_pages=1, max_items=3,
                                    keep_contact_name=False, log=lambda message: None)
        self.assertEqual(stats['ok'], 3)
        self.assertEqual(self.store.count('done'), 3)
        self.assertEqual(reason, 'Reached the property limit.')
        self.assertTrue(all(record['Contact_Name'] == '' for record in self.store.listings()))

    def test_sparse_records_and_large_vnd_precision(self):
        item, parsed = self.queue()
        for key in ('Province', 'District', 'Ward', 'Contact_Name', 'Legal_Status', 'Direction'):
            parsed[key] = ''
        parsed.update(Price_VND=9007199254740993, Area=None, Verified_Status=None)
        self.store.save_listing(item, parsed)
        saved = self.store._one('SELECT * FROM dbo.batdongsan_data WHERE Post_ID=?', parsed['Post_ID'])
        self.assertEqual(saved['Price_VND'], 9007199254740993)
        self.assertIsNone(saved['Area'])
        self.assertIsNone(saved['Verified_Status'])
        self.assertEqual(saved['Seller_Type'], parsed['Seller_Type'])

    def test_schema_reuse_retains_existing_records(self):
        item, parsed = self.queue()
        self.store.save_listing(item, parsed)
        storage.create_schema(self.store.connection)
        self.assertEqual(list(self.store.listings()), [parsed])


if __name__ == '__main__':
    unittest.main()

"""SQL Server storage for property records and resumable, run-scoped crawl state."""

import hashlib
import json
import math
import re
import struct
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import pyodbc

DEFAULT_SERVER = r'.\SQLEXPRESS'
DEFAULT_DATABASE = 'ADY_Project'
DEFAULT_DRIVER = 'ODBC Driver 18 for SQL Server'
HOSTS = ('batdongsan.com.vn', 'www.batdongsan.com.vn')
PRODUCT_PATH_RE = re.compile(r'-pr(\d+)/?$')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def valid_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Only HTTPS URLs on batdongsan.com.vn are accepted.') from None
    return url


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), '%d/%m/%Y').date()
    except ValueError:
        return None


def parse_bool(value):
    return {'true': 1, '1': 1, 'false': 0, '0': 0}.get(str(value).strip().lower())


def parse_number(value, number_type=float):
    if value is None or str(value).strip() == '':
        return None
    try:
        number = number_type(str(value).strip())
        if isinstance(number, Decimal) and not number.is_finite():
            return None
        return number if number_type is not float or math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return None


def _odbc_value(value):
    """Quote ODBC values so server/driver names cannot inject extra settings."""
    return '{' + str(value).replace('}', '}}') + '}'


def _datetimeoffset(raw):
    """Decode SQL_SS_TIMESTAMPOFFSET, which pyodbc does not convert by default."""
    year, month, day, hour, minute, second, nanoseconds, offset_hours, offset_minutes = struct.unpack('<6hI2h', raw)
    offset = timezone(timedelta(hours=offset_hours, minutes=offset_minutes))
    return datetime(year, month, day, hour, minute, second, nanoseconds // 1000, offset)


def connect_database(server=DEFAULT_SERVER, database=DEFAULT_DATABASE, driver=DEFAULT_DRIVER):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', database):
        raise ValueError('Database names must start with a letter/underscore and use letters, digits or underscores.')
    settings = (f'DRIVER={_odbc_value(driver)};SERVER={_odbc_value(server)};'
                f'DATABASE={_odbc_value(database)};Trusted_Connection=yes;'
                'Encrypt=yes;TrustServerCertificate=yes;')
    connection = pyodbc.connect(settings, timeout=10, autocommit=False)
    connection.add_output_converter(-155, _datetimeoffset)
    connection.timeout = 30
    return connection


def _acquire_lock(cursor, resource, owner='Session'):
    result = cursor.execute(
        "DECLARE @result int; EXEC @result = sys.sp_getapplock "
        "@Resource=?, @LockMode='Exclusive', @LockOwner=?, @LockTimeout=0; SELECT @result;",
        resource, owner).fetchone()[0]
    if result < 0:
        raise RuntimeError(f'Another scraper is using this resource: {resource}.')


def create_schema(connection):
    """Apply additive schema changes in one transaction, including the data view."""
    cursor = connection.cursor()
    try:
        _acquire_lock(cursor, 'ADYTask11:schema', 'Transaction')
        script = Path(__file__).with_name('sqlserver_schema.sql').read_text(encoding='utf-8')
        for batch in re.split(r'^\s*GO\s*$', script, flags=re.M | re.I):
            if batch.strip():
                cursor.execute(batch)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()


def get_or_create(cursor, table, id_column, values):
    """Identifiers are fixed by this module; all data values are parameters."""
    if any(value is None or value == '' for value in values.values()):
        return None
    where = ' AND '.join(f'{column} = ?' for column in values)
    existing = cursor.execute(
        f'SELECT {id_column} FROM dbo.{table} WITH (UPDLOCK, HOLDLOCK) WHERE {where}',
        tuple(values.values())).fetchone()
    if existing:
        return existing[0]
    columns = ', '.join(values)
    placeholders = ', '.join('?' for _ in values)
    return cursor.execute(
        f'INSERT INTO dbo.{table} ({columns}) OUTPUT INSERTED.{id_column} '
        f'VALUES ({placeholders})', tuple(values.values())).fetchone()[0]


def upsert_record(cursor, table, unique_column, values):
    key = values[unique_column]
    existing = cursor.execute(
        f'SELECT 1 FROM dbo.{table} WITH (UPDLOCK, HOLDLOCK) WHERE {unique_column} = ?', key).fetchone()
    if existing:
        changes = {column: value for column, value in values.items() if column != unique_column}
        updates = ', '.join(f'{column} = ?' for column in changes)
        cursor.execute(f'UPDATE dbo.{table} SET {updates} WHERE {unique_column} = ?',
                       (*changes.values(), key))
    else:
        columns = ', '.join(values)
        placeholders = ', '.join('?' for _ in values)
        cursor.execute(f'INSERT INTO dbo.{table} ({columns}) VALUES ({placeholders})',
                       tuple(values.values()))


def save_property(cursor, row):
    """Write normalized property data using the caller's transaction; never commit here."""
    post_id, title, url = (clean(row.get(key)) for key in ('Post_ID', 'Title', 'URL'))
    if not post_id or not title or not url:
        raise ValueError('Post_ID, Title and URL are required.')
    valid_url(url)
    province, district, ward = (clean(row.get(key)) for key in ('Province', 'District', 'Ward'))
    province_id = get_or_create(cursor, 'provinces', 'province_id', {'province_name': province})
    district_id = get_or_create(cursor, 'districts', 'district_id',
                                {'province_id': province_id, 'district_name': district})
    ward_id = get_or_create(cursor, 'wards', 'ward_id', {'district_id': district_id, 'ward_name': ward})
    contact_id = get_or_create(cursor, 'contacts', 'contact_id', {
        'contact_name': clean(row.get('Contact_Name')),
        'seller_type': clean(row.get('Seller_Type')) or 'Unknown',
    })
    legal_id = get_or_create(cursor, 'legal_statuses', 'legal_status_id',
                             {'status_name': clean(row.get('Legal_Status'))})
    direction_id = get_or_create(cursor, 'directions', 'direction_id',
                                 {'direction_name': clean(row.get('Direction'))})
    upsert_record(cursor, 'listings', 'post_id', {
        'post_id': post_id, 'title': title, 'address_detail': row.get('Address'),
        'ward_id': ward_id, 'contact_id': contact_id, 'legal_status_id': legal_id,
        'direction_id': direction_id, 'price_text': row.get('Price_Text'),
        'price_vnd': parse_number(row.get('Price_VND'), int),
        'price_per_m2': parse_number(row.get('Price_Per_M2'), int),
        'price_period': row.get('Price_Period'), 'area': parse_number(row.get('Area'), Decimal),
        'frontage': parse_number(row.get('Frontage'), Decimal),
        'road_width': parse_number(row.get('Road_Width'), Decimal),
        'is_corner_lot': parse_bool(row.get('Is_Corner_Lot')),
        'verified_status': parse_bool(row.get('Verified_Status')),
        'post_date': parse_date(row.get('Post_Date')),
        'expiration_date': parse_date(row.get('Expiration_Date')), 'source_url': url,
    })
    listing_id = cursor.execute('SELECT listing_id FROM dbo.listings WHERE post_id = ?', post_id).fetchone()[0]
    upsert_record(cursor, 'listing_audits', 'listing_id', {
        'listing_id': listing_id, 'price_per_m2_source': row.get('Price_Per_M2_Source'),
        'corner_keywords': row.get('Corner_Keywords'), 'seller_keywords': row.get('Seller_Keywords'),
        'province_text': province, 'district_text': district, 'ward_text': ward,
        'seller_type_text': clean(row.get('Seller_Type')),
        'scraped_at': row.get('Scraped_At'), 'source_list_url': row.get('Source_List_URL'),
    })
    return listing_id


class SqlServerStore:
    """One runner per run. Every method ends its transaction before network activity."""

    def __init__(self, server=DEFAULT_SERVER, database=DEFAULT_DATABASE, driver=DEFAULT_DRIVER,
                 run_id=None, list_url=None, create_run=True):
        self.connection = connect_database(server, database, driver)
        self.run_id = None
        self._run_lock_acquired = False
        self.server, self.database = server, database
        try:
            create_schema(self.connection)
            if not create_run:
                return
            if run_id is not None:
                self.run_id = str(uuid.UUID(str(run_id)))
                with self._transaction() as cursor:
                    _acquire_lock(cursor, 'ADYTask11:run:' + self.run_id)
                    self._run_lock_acquired = True
                    row = cursor.execute('SELECT list_url FROM dbo.crawl_runs WHERE run_id=?', self.run_id).fetchone()
                    if row is None:
                        raise ValueError(f'No saved SQL Server run exists with ID {self.run_id}.')
                    if list_url is not None and row[0] != list_url:
                        raise ValueError('The saved run uses a different list URL. Start a new run instead.')
                    cursor.execute('UPDATE dbo.crawl_runs SET last_started_at=? WHERE run_id=?', utc_now(), self.run_id)
            else:
                if not list_url:
                    raise ValueError('A list URL is required for a new run.')
                valid_url(list_url)
                self.run_id = str(uuid.uuid4())
                with self._transaction() as cursor:
                    _acquire_lock(cursor, 'ADYTask11:run:' + self.run_id)
                    self._run_lock_acquired = True
                    cursor.execute('INSERT INTO dbo.crawl_runs(run_id,list_url,created_at,last_started_at) VALUES(?,?,?,?)',
                                   self.run_id, list_url, utc_now(), utc_now())
                    self._add_list_page(cursor, list_url)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    @contextmanager
    def _transaction(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    @staticmethod
    def _dict(cursor, row):
        return dict(zip((column[0] for column in cursor.description), row)) if row is not None else None

    def _one(self, sql, *params):
        with self._transaction() as cursor:
            cursor.execute(sql, *params)
            return self._dict(cursor, cursor.fetchone())

    def _many(self, sql, *params):
        with self._transaction() as cursor:
            cursor.execute(sql, *params)
            return [self._dict(cursor, row) for row in cursor.fetchall()]

    def close(self):
        # Explicitly release session locks because ODBC may pool a closed connection.
        try:
            if self._run_lock_acquired:
                with self._transaction() as cursor:
                    cursor.execute("EXEC sys.sp_releaseapplock @Resource=?, @LockOwner='Session';",
                                   'ADYTask11:run:' + self.run_id)
                self._run_lock_acquired = False
        finally:
            self.connection.close()

    def get_meta(self, key, default=None):
        if key not in ('list_url', 'created_at', 'last_stop_reason'):
            return default
        row = self._one(f'SELECT {key} AS value FROM dbo.crawl_runs WHERE run_id=?', self.run_id)
        return row['value'] if row is not None and row['value'] is not None else default

    def finish(self, reason):
        with self._transaction() as cursor:
            cursor.execute('UPDATE dbo.crawl_runs SET last_stop_reason=? WHERE run_id=?', reason[:2048], self.run_id)

    def runs(self):
        return self._many('SELECT TOP (20) r.run_id,r.created_at,r.list_url,r.last_stop_reason,'
                          "(SELECT COUNT(*) FROM dbo.crawl_queue q WHERE q.run_id=r.run_id AND q.status='done') AS done "
                          'FROM dbo.crawl_runs r ORDER BY r.created_at DESC,r.run_id')

    @staticmethod
    def _hash(url):
        return hashlib.sha256(url.encode('utf-8')).digest()

    def _add_list_page(self, cursor, url):
        valid_url(url)
        digest = self._hash(url)
        exists = cursor.execute('SELECT url FROM dbo.crawl_list_pages WITH (UPDLOCK,HOLDLOCK) '
                                'WHERE run_id=? AND url_hash=?', self.run_id, digest).fetchone()
        if exists:
            if exists[0] != url:
                raise ValueError('List-page URL hash collision.')
            return
        cursor.execute("INSERT INTO dbo.crawl_list_pages(run_id,url,url_hash,status) VALUES(?,?,?,'pending')",
                       self.run_id, url, digest)

    def add_list_page(self, url):
        with self._transaction() as cursor:
            self._add_list_page(cursor, url)

    def next_list_page(self):
        return self._one("SELECT TOP (1) * FROM dbo.crawl_list_pages WHERE run_id=? AND status='pending' ORDER BY seq",
                         self.run_id)

    def pages_done(self):
        return self._one("SELECT COUNT(*) AS n FROM dbo.crawl_list_pages WHERE run_id=? AND status='done'",
                         self.run_id)['n']

    def record_list_page(self, page_url, detail_urls, next_url):
        detail_urls = list(detail_urls)
        signature = hashlib.sha1('\n'.join(detail_urls).encode()).hexdigest()
        new = 0
        with self._transaction() as cursor:
            self._add_list_page(cursor, page_url)
            page = cursor.execute('SELECT status FROM dbo.crawl_list_pages WITH (UPDLOCK,HOLDLOCK) '
                                  'WHERE run_id=? AND url_hash=?', self.run_id, self._hash(page_url)).fetchone()
            if page[0] == 'done':
                return 0, True
            repeated = bool(detail_urls) and cursor.execute(
                "SELECT 1 FROM dbo.crawl_list_pages WHERE run_id=? AND status='done' AND signature=?",
                self.run_id, signature).fetchone() is not None
            if not repeated:
                for url in detail_urls:
                    valid_url(url)
                    match = PRODUCT_PATH_RE.search(urlsplit(url).path)
                    if match is None:
                        raise ValueError(f'Queue URL has no Post_ID: {url}')
                    exists = cursor.execute('SELECT 1 FROM dbo.crawl_queue WITH (UPDLOCK,HOLDLOCK) '
                                            'WHERE run_id=? AND post_id=?', self.run_id, match[1]).fetchone()
                    if not exists:
                        cursor.execute('INSERT INTO dbo.crawl_queue '
                                       '(run_id,url,url_hash,post_id,source_list_url,status,attempts,updated_at) '
                                       "VALUES(?,?,?,?,?,'pending',0,?)",
                                       self.run_id, url, self._hash(url), match[1], page_url, utc_now())
                        new += 1
                if next_url:
                    self._add_list_page(cursor, next_url)
            cursor.execute("UPDATE dbo.crawl_list_pages SET status='done',signature=? WHERE run_id=? AND url_hash=?",
                           signature, self.run_id, self._hash(page_url))
        return new, repeated

    def next_pending(self):
        return self._one("SELECT TOP (1) * FROM dbo.crawl_queue WHERE run_id=? AND status='pending' ORDER BY seq",
                         self.run_id)

    def mark(self, url, status, error=''):
        if status not in ('failed', 'gone', 'skipped', 'duplicate'):
            raise ValueError('Invalid status; only save_listing may mark a property done.')
        with self._transaction() as cursor:
            cursor.execute('UPDATE dbo.crawl_queue SET status=?,error=?,attempts=attempts+?,updated_at=? '
                           "WHERE run_id=? AND url_hash=? AND url=? AND status='pending'",
                           status, error, 1 if status == 'failed' else 0, utc_now(), self.run_id, self._hash(url), url)

    def save_listing(self, item, row):
        """Commit property, per-run snapshot and done status together; rollback on failure."""
        with self._transaction() as cursor:
            cursor.execute('SELECT * FROM dbo.crawl_queue WITH (UPDLOCK,HOLDLOCK) '
                           'WHERE run_id=? AND url_hash=? AND url=?', self.run_id, self._hash(item['url']), item['url'])
            queued = self._dict(cursor, cursor.fetchone())
            if queued is None:
                raise ValueError('Cannot save a property outside this run\'s queue.')
            if queued['status'] != 'pending':
                return False
            expected = queued['post_id']
            actual = clean(row.get('Post_ID'))
            try:
                valid_url(row.get('URL', ''))
                match = PRODUCT_PATH_RE.search(urlsplit(row['URL']).path)
            except (ValueError, TypeError, KeyError):
                match = None
            problem = ''
            if str(item['post_id']) != expected or actual != expected or not match or match[1] != expected:
                problem = f'Post_ID mismatch: queue={expected}, parser={actual}, URL={row.get("URL", "")}'
            elif not row.get('Scraped_At'):
                problem = 'Scraped_At is required; incomplete properties are not saved.'
            elif row.get('Source_List_URL') != queued['source_list_url']:
                problem = 'Source_List_URL does not match the queued source page.'
            elif cursor.execute('SELECT 1 FROM dbo.listings WHERE source_url=? AND post_id<>?',
                                row['URL'], expected).fetchone():
                problem = 'This URL already belongs to another Post_ID.'
            if problem:
                cursor.execute("UPDATE dbo.crawl_queue SET status='failed',error=?,attempts=attempts+1,updated_at=? "
                               'WHERE run_id=? AND post_id=?', problem, utc_now(), self.run_id, expected)
                return False
            saved = dict(row, Post_ID=expected)
            payload = json.dumps(saved, ensure_ascii=False, allow_nan=False)
            listing_id = save_property(cursor, saved)
            cursor.execute('INSERT INTO dbo.crawl_results '
                           '(run_id,post_id,listing_id,scraped_at,source_list_url,data) VALUES(?,?,?,?,?,?)',
                           self.run_id, expected, listing_id, saved['Scraped_At'], saved['Source_List_URL'], payload)
            cursor.execute("UPDATE dbo.crawl_queue SET status='done',error='',updated_at=? WHERE run_id=? AND post_id=?",
                           utc_now(), self.run_id, expected)
        return True

    def save_refreshed_property(self, row, expected_post_id):
        """Save a direct refresh atomically without changing crawl queues or snapshots."""
        valid_url(row.get('URL', ''))
        match = PRODUCT_PATH_RE.search(urlsplit(row['URL']).path)
        if (not match or match[1] != str(expected_post_id)
                or clean(row.get('Post_ID')) != str(expected_post_id)):
            raise ValueError('The refresh URL and Post_ID must identify the same property.')
        if not row.get('Scraped_At'):
            raise ValueError('Scraped_At is required for a refresh.')

        with self._transaction() as cursor:
            conflict = cursor.execute(
                'SELECT 1 FROM dbo.listings WHERE source_url=? AND post_id<>?',
                row['URL'], str(expected_post_id)).fetchone()
            if conflict:
                raise ValueError('This URL already belongs to another Post_ID.')
            listing_id = save_property(cursor, row)
        return listing_id

    def reset_failed(self, max_attempts=3):
        if not isinstance(max_attempts, int) or max_attempts < 0:
            raise ValueError('max_attempts must be a nonnegative integer.')
        with self._transaction() as cursor:
            cursor.execute("UPDATE dbo.crawl_queue SET status='pending',updated_at=? "
                           "WHERE run_id=? AND status='failed' AND attempts<?", utc_now(), self.run_id, max_attempts)
            return cursor.rowcount
    
    def count(self, status):
        return self._one('SELECT COUNT(*) AS n FROM dbo.crawl_queue WHERE run_id=? AND status=?',
                         self.run_id, status)['n']

    def total_found(self):
        return self._one('SELECT COUNT(*) AS n FROM dbo.crawl_queue WHERE run_id=?', self.run_id)['n']

    def listings(self):
        rows = self._many('SELECT data FROM dbo.crawl_results WHERE run_id=? ORDER BY listing_id', self.run_id)
        return (json.loads(row['data']) for row in rows)

    def issues(self):
        return self._many('SELECT url,status,attempts,error,source_list_url FROM dbo.crawl_queue '
                          "WHERE run_id=? AND status NOT IN ('done','pending') ORDER BY seq", self.run_id)

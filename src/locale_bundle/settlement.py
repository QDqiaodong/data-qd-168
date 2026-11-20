"""Offline usage reconciliation and settlement (contract v7).

Headquarters settles monthly with translation vendors based on the release
snapshots that stores actually rendered. Stores keep operating while offline
and upload cumulative counter vectors stamped at UTC midnights; uploads may
miss days, arrive out of order and be corrected later by re-uploading the
same midnight with a higher revision number. The whole settlement state (the
store registry, registered v6 snapshots, billing agreements, raw counter
receipts and the closed monthly bills) lives in one local JSON file, the
"books", and every command validates the next state completely before
replacing that file atomically.

Reconciliation is per business day: a day's usage is the difference of the
two current vectors at the adjacent midnights and only exists when both
endpoints are present; gaps are never zero-filled, averaged or smeared into
a month. Finance closes month by month in order from the earliest enable
month. A closing recomputes every service month up to the closed one from
the currently accepted receipts, books the current service month for the
first time and settles the remaining differences of earlier service months
as adjustments (negative ones as offsets) on the new bill. Closed bills are
frozen together with the receipt horizon and the pinned revisions they used,
so a late or corrected counter can only move money through a later bill and
never rewrites a bill that was already handed to a vendor.
"""
import calendar
import datetime
import re

from .common import fields, text, integer, array
from .delivery import validate_snapshot

STORE_LIMIT = 1000
SNAPSHOT_LIMIT = 1000
AGREEMENT_LIMIT = 10000
RECEIPT_LIMIT = 10000
RECORD_LIMIT = 10000
COUNT_LIMIT = 10000
BILL_LIMIT = 2400
ENTRY_LIMIT = 10000
LINE_LIMIT = 10000
BASIS_ROW_LIMIT = 100000
AMOUNT_LIMIT = 10**15

ENTRY_KINDS = ('current', 'adjustment', 'offset')
MISSING_ENDPOINTS = ('left', 'right', 'both')

_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_MONTH_RE = re.compile(r'[0-9]{4}-[0-9]{2}')


class SettlementError(ValueError):
    """A validation or conflict error that fails the whole command."""


# --------------------------------------------------------------------------
# UTC dates and service months
# --------------------------------------------------------------------------

def _date(value):
    text(value)
    if not _DATE_RE.fullmatch(value):
        raise SettlementError('invalid UTC date: %r' % (value,))
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise SettlementError('invalid UTC date: %r' % (value,))
    return value


def _month(value):
    text(value)
    if not _MONTH_RE.fullmatch(value):
        raise SettlementError('invalid service month: %r' % (value,))
    if not 1 <= int(value[5:7]) <= 12:
        raise SettlementError('invalid service month: %r' % (value,))
    return value


def _day_after(value):
    return (datetime.date.fromisoformat(value)
            + datetime.timedelta(days=1)).isoformat()


def _month_of(date_value):
    return date_value[:7]


def _month_start(month):
    return month + '-01'


def _month_end(month):
    year, mon = (int(part) for part in month.split('-'))
    return '%04d-%02d-%02d' % (year, mon, calendar.monthrange(year, mon)[1])


def _add_months(month, count):
    year, mon = (int(part) for part in month.split('-'))
    index = year * 12 + mon - 1 + count
    return '%04d-%02d' % (index // 12, index % 12 + 1)


def _month_diff(earlier, later):
    ey, em = (int(part) for part in earlier.split('-'))
    ly, lm = (int(part) for part in later.split('-'))
    return (ly - ey) * 12 + (lm - em)


def _countable_dates(store, month):
    """The days of one service month a store must account for (from enable)."""
    dates = []
    date = max(store['enabled'], _month_start(month))
    end = _month_end(month)
    while date <= end:
        dates.append(date)
        date = _day_after(date)
    return dates


# --------------------------------------------------------------------------
# Rows of the books file
# --------------------------------------------------------------------------

def _store_row(row):
    fields(row, 'store_id enabled')
    return {'store_id': text(row['store_id']), 'enabled': _date(row['enabled'])}


def _agreement_row(row):
    fields(row, 'translation_id review_seq vendor unit_price')
    return {'translation_id': text(row['translation_id']),
            'review_seq': integer(row['review_seq'], 1),
            'vendor': text(row['vendor']),
            'unit_price': integer(row['unit_price'], 0)}


def _agreement_key(agreement):
    return (agreement['translation_id'], agreement['review_seq'])


def _dimension(row):
    fields(row, 'snapshot_id application_id locale key count')
    return {'snapshot_id': text(row['snapshot_id']),
            'application_id': text(row['application_id']),
            'locale': text(row['locale']),
            'key': text(row['key']),
            'count': integer(row['count'], 0)}


def _dim_key(dim):
    return (dim['snapshot_id'], dim['application_id'], dim['locale'],
            dim['key'])


def _counts_rows(rows, stored):
    """Validate one counter vector; batch input is normalized, stored rows
    must already be in normalized form (zeros dropped, sorted by dimension)."""
    dims = [_dimension(row) for row in array(rows, COUNT_LIMIT)]
    keys = [_dim_key(dim) for dim in dims]
    if len(set(keys)) != len(keys):
        raise SettlementError('duplicate count dimension')
    if stored:
        if any(dim['count'] == 0 for dim in dims):
            raise SettlementError('stored counts must drop zero dimensions')
        if keys != sorted(keys):
            raise SettlementError('stored counts must be sorted by dimension')
        return dims
    dims = [dim for dim in dims if dim['count'] > 0]  # explicit zero == omission
    dims.sort(key=_dim_key)
    return dims


def _counts_map(counts):
    return {_dim_key(dim): dim['count'] for dim in counts}


def _receipt_row(row):
    fields(row, 'seq store_id date revision counts')
    return {'seq': integer(row['seq'], 1),
            'store_id': text(row['store_id']),
            'date': _date(row['date']),
            'revision': integer(row['revision'], 1),
            'counts': _counts_rows(row['counts'], stored=True)}


def _record_row(row):
    fields(row, 'store_id date revision counts')
    return {'store_id': text(row['store_id']),
            'date': _date(row['date']),
            'revision': integer(row['revision'], 1),
            'counts': _counts_rows(row['counts'], stored=False)}


def _usage_line(row):
    fields(row, 'snapshot_id application_id locale key count source_locale '
                'translation_id review_seq vendor unit_price amount')
    line = {'snapshot_id': text(row['snapshot_id']),
            'application_id': text(row['application_id']),
            'locale': text(row['locale']),
            'key': text(row['key']),
            'count': integer(row['count'], 1),
            'source_locale': text(row['source_locale']),
            'translation_id': row['translation_id'],
            'review_seq': row['review_seq'],
            'vendor': row['vendor'],
            'unit_price': row['unit_price'],
            'amount': integer(row['amount'], 0, AMOUNT_LIMIT)}
    if line['translation_id'] is None:
        if (line['review_seq'] is not None or line['vendor'] is not None
                or line['unit_price'] is not None or line['amount'] != 0):
            raise SettlementError('default-locale usage cannot carry billing fields')
    else:
        line['translation_id'] = text(line['translation_id'])
        line['review_seq'] = integer(line['review_seq'], 1)
        line['vendor'] = text(line['vendor'])
        line['unit_price'] = integer(line['unit_price'], 0)
        if line['amount'] != line['count'] * line['unit_price']:
            raise SettlementError('usage amount does not match count times price')
    return line


def _usage_lines(rows):
    lines = [_usage_line(row) for row in array(rows, LINE_LIMIT)]
    keys = [_dim_key(line) for line in lines]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise SettlementError('usage lines must be unique and sorted by dimension')
    return lines


def _entry_line(row):
    fields(row, 'translation_id review_seq usage unit_price amount')
    line = {'translation_id': text(row['translation_id']),
            'review_seq': integer(row['review_seq'], 1),
            'usage': integer(row['usage'], 1),
            'unit_price': integer(row['unit_price'], 0),
            'amount': integer(row['amount'], 0, AMOUNT_LIMIT)}
    if line['amount'] != line['usage'] * line['unit_price']:
        raise SettlementError('entry line amount does not match usage times price')
    return line


def _entry(row):
    fields(row, 'service_month vendor kind recognized booked_before amount lines')
    entry = {'service_month': _month(row['service_month']),
             'vendor': text(row['vendor']),
             'kind': row['kind'],
             'recognized': integer(row['recognized'], 0, AMOUNT_LIMIT),
             'booked_before': integer(row['booked_before'], 0, AMOUNT_LIMIT),
             'amount': integer(row['amount'], -AMOUNT_LIMIT, AMOUNT_LIMIT),
             'lines': [_entry_line(line) for line in array(row['lines'], LINE_LIMIT)]}
    if entry['kind'] not in ENTRY_KINDS:
        raise SettlementError('invalid entry kind')
    keys = [(line['translation_id'], line['review_seq']) for line in entry['lines']]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise SettlementError('entry lines must be unique and sorted by approval')
    if sum(line['amount'] for line in entry['lines']) != entry['recognized']:
        raise SettlementError('entry lines do not sum to the recognized amount')
    if entry['amount'] != entry['recognized'] - entry['booked_before']:
        raise SettlementError('entry amount is not recognized minus booked')
    return entry


def _vendor_total(row):
    fields(row, 'vendor amount')
    return {'vendor': text(row['vendor']),
            'amount': integer(row['amount'], -AMOUNT_LIMIT, AMOUNT_LIMIT)}


def _basis_complete_day(row):
    fields(row, 'date left_revision right_revision usage')
    return {'date': _date(row['date']),
            'left_revision': integer(row['left_revision'], 0),
            'right_revision': integer(row['right_revision'], 0),
            'usage': _usage_lines(row['usage'])}


def _basis_missing_day(row):
    fields(row, 'date missing')
    if row['missing'] not in MISSING_ENDPOINTS:
        raise SettlementError('invalid missing endpoint')
    return {'date': _date(row['date']), 'missing': row['missing']}


def _basis_row(row):
    fields(row, 'store_id month complete_days missing_days')
    complete = [_basis_complete_day(day) for day in array(row['complete_days'], 40)]
    missing = [_basis_missing_day(day) for day in array(row['missing_days'], 40)]
    for days in (complete, missing):
        dates = [day['date'] for day in days]
        if len(set(dates)) != len(dates) or dates != sorted(dates):
            raise SettlementError('basis days must be unique and sorted')
    return {'store_id': text(row['store_id']), 'month': _month(row['month']),
            'complete_days': complete, 'missing_days': missing}


def _bill_row(row):
    fields(row, 'month seq receipts_through entries vendor_totals basis')
    bill = {'month': _month(row['month']),
            'seq': integer(row['seq'], 1),
            'receipts_through': integer(row['receipts_through'], 0, RECEIPT_LIMIT),
            'entries': [_entry(entry) for entry in array(row['entries'], ENTRY_LIMIT)],
            'vendor_totals': [_vendor_total(total) for total in
                              array(row['vendor_totals'], ENTRY_LIMIT)],
            'basis': [_basis_row(basis) for basis in
                      array(row['basis'], BASIS_ROW_LIMIT)]}
    keys = [(entry['service_month'], entry['vendor']) for entry in bill['entries']]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise SettlementError('bill entries must be unique and sorted')
    vendors = [total['vendor'] for total in bill['vendor_totals']]
    if len(set(vendors)) != len(vendors) or vendors != sorted(vendors):
        raise SettlementError('bill vendor totals must be unique and sorted')
    for entry in bill['entries']:
        if entry['service_month'] > bill['month']:
            raise SettlementError('entry service month exceeds the bill month')
        if entry['kind'] == 'current':
            if (entry['service_month'] != bill['month']
                    or entry['booked_before'] != 0 or entry['amount'] <= 0):
                raise SettlementError('invalid current-service entry')
        elif entry['service_month'] == bill['month']:
            raise SettlementError('adjustments must name an earlier service month')
        elif entry['kind'] == 'offset' and entry['amount'] >= 0:
            raise SettlementError('an offset entry must book a negative amount')
        elif entry['kind'] == 'adjustment' and entry['amount'] <= 0:
            raise SettlementError('an adjustment entry must book a positive amount')
    return bill


# --------------------------------------------------------------------------
# Supply evidence and billing attribution
# --------------------------------------------------------------------------

def _supply_index(snapshots):
    """snapshot_id -> (application_id, locale, key) -> the supplied message."""
    index = {}
    for snapshot in snapshots:
        per_snapshot = {}
        for package in snapshot['packages']:
            for message in package['messages']:
                per_snapshot[(package['application_id'], package['locale'],
                              message['key'])] = message
        index[snapshot['snapshot_id']] = per_snapshot
    return index


def _check_dimension(supply, agreements, dim):
    """One count dimension must resolve to a registered snapshot entry, and
    non-default supply must have a billing agreement (even a zero-priced one)."""
    per_snapshot = supply.get(dim['snapshot_id'])
    if per_snapshot is None:
        raise SettlementError('count references an unregistered snapshot: '
                              + dim['snapshot_id'])
    message = per_snapshot.get((dim['application_id'], dim['locale'], dim['key']))
    if message is None:
        raise SettlementError('count references an entry the snapshot does not '
                              'serve: %s/%s/%s' % (dim['application_id'],
                                                   dim['locale'], dim['key']))
    evidence = (message['translation_id'], message['review_seq'])
    if message['translation_id'] is not None and evidence not in agreements:
        raise SettlementError('non-default supply lacks a billing agreement: '
                              '%s#%d' % evidence)


def _usage_delta(left, right, supply, agreements):
    """Positive per-dimension deltas between two vectors, with the actual
    supply and billing attribution taken from the registered snapshot."""
    lines = []
    for dim_key in sorted(set(left) | set(right)):
        delta = right.get(dim_key, 0) - left.get(dim_key, 0)
        if delta <= 0:
            continue
        snapshot_id, application_id, locale, key = dim_key
        message = supply[snapshot_id][(application_id, locale, key)]
        line = {'snapshot_id': snapshot_id, 'application_id': application_id,
                'locale': locale, 'key': key, 'count': delta,
                'source_locale': message['source_locale'],
                'translation_id': message['translation_id'],
                'review_seq': message['review_seq'],
                'vendor': None, 'unit_price': None, 'amount': 0}
        if message['translation_id'] is not None:
            agreement = agreements[(message['translation_id'],
                                    message['review_seq'])]
            line['vendor'] = agreement['vendor']
            line['unit_price'] = agreement['unit_price']
            line['amount'] = delta * agreement['unit_price']
        lines.append(line)
    return lines


# --------------------------------------------------------------------------
# Current vectors, monotonicity, day reconciliation
# --------------------------------------------------------------------------

def _current_vectors(store, receipts):
    """date -> (revision, counts) of the accepted highest revisions, seeded
    with the fixed all-zero vector (revision 0) at the enable-date midnight."""
    vectors = {store['enabled']: (0, {})}
    for receipt in receipts:
        if receipt['store_id'] != store['store_id']:
            continue
        current = vectors.get(receipt['date'])
        if current is None or receipt['revision'] > current[0]:
            vectors[receipt['date']] = (receipt['revision'],
                                        _counts_map(receipt['counts']))
    return vectors


def _check_monotonic(stores, receipts):
    """Along increasing time every dimension of the current vectors must be
    non-decreasing per store, also across unreported midnights."""
    for store in stores:
        vectors = _current_vectors(store, receipts)
        previous_date, previous = None, None
        for date in sorted(vectors):
            counts = vectors[date][1]
            if previous is not None:
                for dim in sorted(set(previous) | set(counts)):
                    if counts.get(dim, 0) < previous.get(dim, 0):
                        raise SettlementError(
                            'count regression for store %s between %s and %s: '
                            'dimension %s/%s/%s/%s'
                            % (store['store_id'], previous_date, date, *dim))
            previous_date, previous = date, counts


def _store_days(store, vectors, month, supply, agreements):
    """Reconcile one store's countable days of one month into (complete,
    missing) day lists; a day is complete only when both adjacent midnight
    vectors exist, and its usage is their difference."""
    complete, missing = [], []
    for date in _countable_dates(store, month):
        right_date = _day_after(date)
        left = vectors.get(date)
        right = vectors.get(right_date)
        if left is not None and right is not None:
            complete.append({'date': date,
                             'left_revision': left[0],
                             'right_revision': right[0],
                             'usage': _usage_delta(left[1], right[1], supply,
                                                   agreements)})
        else:
            which = ('both' if left is None and right is None
                     else 'left' if left is None else 'right')
            missing.append({'date': date, 'missing': which})
    return complete, missing


# --------------------------------------------------------------------------
# Recognized amounts, bill entries, vendor totals
# --------------------------------------------------------------------------

def _recognized_from_basis(basis):
    """(service_month, vendor) -> per-approval recognized usage and amount."""
    recognized = {}
    for row in basis:
        for day in row['complete_days']:
            for line in day['usage']:
                if line['vendor'] is None:
                    continue
                lines = recognized.setdefault((row['month'], line['vendor']), {})
                key = (line['translation_id'], line['review_seq'])
                slot = lines.setdefault(key, {'usage': 0,
                                              'unit_price': line['unit_price'],
                                              'amount': 0})
                slot['usage'] += line['count']
                slot['amount'] += line['amount']
    return recognized


def _entries(recognized, booked, bill_month):
    """Bill entries: the current service month is booked for the first time;
    earlier service months settle only their remaining difference, negative
    ones as offsets. Combinations whose recognized amount already matches the
    booked total are not booked again."""
    entries = []
    for service_month, vendor in sorted(set(recognized) | set(booked)):
        lines_map = recognized.get((service_month, vendor), {})
        amount = sum(slot['amount'] for slot in lines_map.values())
        before = booked.get((service_month, vendor), 0)
        if amount == before:
            continue
        lines = [{'translation_id': translation_id, 'review_seq': review_seq,
                  'usage': slot['usage'], 'unit_price': slot['unit_price'],
                  'amount': slot['amount']}
                 for (translation_id, review_seq), slot in sorted(lines_map.items())]
        if service_month == bill_month:
            kind = 'current'
        else:
            kind = 'offset' if amount < before else 'adjustment'
        entries.append({'service_month': service_month, 'vendor': vendor,
                        'kind': kind, 'recognized': amount,
                        'booked_before': before, 'amount': amount - before,
                        'lines': lines})
    return entries


def _vendor_totals(entries):
    totals = {}
    for entry in entries:
        totals[entry['vendor']] = totals.get(entry['vendor'], 0) + entry['amount']
    return [{'vendor': vendor, 'amount': totals[vendor]}
            for vendor in sorted(totals)]


def _booked_amounts(bills):
    """Cumulative amounts already booked per (service month, vendor)."""
    booked = {}
    for bill in bills:
        for entry in bill['entries']:
            key = (entry['service_month'], entry['vendor'])
            booked[key] = booked.get(key, 0) + entry['amount']
    return booked


# --------------------------------------------------------------------------
# Books validation
# --------------------------------------------------------------------------

def _vector_at(store, date, revision, by_key, by_date):
    """The pinned vector a frozen bill basis used; revision 0 is the fixed
    all-zero vector of the enable-date midnight."""
    if revision == 0:
        if date != store['enabled']:
            raise SettlementError('revision 0 is only the enable-date zero vector')
        return {}
    receipt = by_key.get((store['store_id'], date, revision))
    if receipt is None:
        raise SettlementError('bill basis references an unknown receipt: '
                              '%s at %s revision %d'
                              % (store['store_id'], date, revision))
    current = max(by_date[(store['store_id'], date)],
                  key=lambda row: row['revision'])
    if current['revision'] != revision:
        raise SettlementError('bill basis revision was not the current one at '
                              'closing: %s at %s revision %d'
                              % (store['store_id'], date, revision))
    return _counts_map(receipt['counts'])


def _verify_bill(bill, stores, receipts, agreements, supply, booked):
    """Re-verify a frozen bill against the receipt horizon it pinned: the
    basis must cover exactly the service months and countable days, complete
    days must reproduce their usage from the pinned revisions, gaps must
    match the horizon, and the entries and totals must follow from the basis
    and the previously booked amounts."""
    through = bill['receipts_through']
    if through > len(receipts):
        raise SettlementError('bill horizon exceeds the receipt log')
    by_date, by_key = {}, {}
    for receipt in receipts[:through]:
        by_date.setdefault((receipt['store_id'], receipt['date']), []).append(receipt)
        by_key[(receipt['store_id'], receipt['date'], receipt['revision'])] = receipt

    def has_vector(store, date):
        # The enable-date midnight always has the fixed all-zero vector.
        return date == store['enabled'] or (store['store_id'], date) in by_date
    store_by_id = {store['store_id']: store for store in stores}
    expected_rows = set()
    for store in stores:
        service = _month_of(store['enabled'])
        while _month_diff(service, bill['month']) >= 0:
            expected_rows.add((store['store_id'], service))
            service = _add_months(service, 1)
    actual_rows = [(row['store_id'], row['month']) for row in bill['basis']]
    if len(set(actual_rows)) != len(actual_rows) or actual_rows != sorted(actual_rows):
        raise SettlementError('bill basis rows must be unique and sorted')
    if set(actual_rows) != expected_rows:
        raise SettlementError('bill basis does not cover exactly the service months')
    for row in bill['basis']:
        store = store_by_id[row['store_id']]
        dates = ([day['date'] for day in row['complete_days']]
                 + [day['date'] for day in row['missing_days']])
        if sorted(dates) != _countable_dates(store, row['month']):
            raise SettlementError('bill basis days do not cover the service month')
        for day in row['complete_days']:
            left = _vector_at(store, day['date'], day['left_revision'],
                              by_key, by_date)
            right = _vector_at(store, _day_after(day['date']),
                               day['right_revision'], by_key, by_date)
            if _usage_delta(left, right, supply, agreements) != day['usage']:
                raise SettlementError('bill basis usage does not match the '
                                      'pinned receipts')
        for day in row['missing_days']:
            left_exists = has_vector(store, day['date'])
            right_exists = has_vector(store, _day_after(day['date']))
            expected = ('both' if not left_exists and not right_exists
                        else 'left' if not left_exists else 'right')
            if day['missing'] != expected:
                raise SettlementError('bill basis gap does not match the '
                                      'receipt horizon')
    if _entries(_recognized_from_basis(bill['basis']), booked,
                bill['month']) != bill['entries']:
        raise SettlementError('bill entries do not follow from the frozen basis')
    if _vendor_totals(bill['entries']) != bill['vendor_totals']:
        raise SettlementError('bill vendor totals do not match its entries')


def validate_books(raw):
    """Validate a books file and return a normalized, freely usable copy."""
    fields(raw, 'version stores snapshots agreements receipts bills')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise SettlementError('unsupported books version')
    stores = [_store_row(row) for row in array(raw['stores'], STORE_LIMIT)]
    store_ids = [store['store_id'] for store in stores]
    if not stores or len(set(store_ids)) != len(store_ids) \
            or store_ids != sorted(store_ids):
        raise SettlementError('stores must be nonempty, unique and sorted')
    snapshots = [validate_snapshot(row)
                 for row in array(raw['snapshots'], SNAPSHOT_LIMIT)]
    snapshot_ids = [snapshot['snapshot_id'] for snapshot in snapshots]
    if len(set(snapshot_ids)) != len(snapshot_ids) \
            or snapshot_ids != sorted(snapshot_ids):
        raise SettlementError('snapshots must be unique and sorted by snapshot_id')
    agreements = [_agreement_row(row)
                  for row in array(raw['agreements'], AGREEMENT_LIMIT)]
    agreement_keys = [_agreement_key(agreement) for agreement in agreements]
    if len(set(agreement_keys)) != len(agreement_keys) \
            or agreement_keys != sorted(agreement_keys):
        raise SettlementError('agreements must be unique and sorted by approval')
    receipts = [_receipt_row(row) for row in array(raw['receipts'], RECEIPT_LIMIT)]
    for index, receipt in enumerate(receipts):
        if receipt['seq'] != index + 1:
            raise SettlementError('receipt seqs must be 1..n in arrival order')

    supply = _supply_index(snapshots)
    agreement_by_key = {_agreement_key(agreement): agreement
                        for agreement in agreements}
    store_by_id = {store['store_id']: store for store in stores}
    seen_revisions = set()
    for receipt in receipts:
        store = store_by_id.get(receipt['store_id'])
        if store is None:
            raise SettlementError('receipt for an unregistered store: '
                                  + receipt['store_id'])
        if receipt['date'] <= store['enabled']:
            raise SettlementError('record date must be after the store enable '
                                  'date: %s at %s'
                                  % (receipt['store_id'], receipt['date']))
        identity = (receipt['store_id'], receipt['date'], receipt['revision'])
        if identity in seen_revisions:
            raise SettlementError('duplicate receipt revision')
        seen_revisions.add(identity)
        for dim in receipt['counts']:
            _check_dimension(supply, agreement_by_key, dim)
    _check_monotonic(stores, receipts)

    bills = [_bill_row(row) for row in array(raw['bills'], BILL_LIMIT)]
    earliest = min(_month_of(store['enabled']) for store in stores)
    booked = {}
    for index, bill in enumerate(bills):
        if bill['seq'] != index + 1:
            raise SettlementError('bill seqs must be 1..n in closing order')
        if bill['month'] != _add_months(earliest, index):
            raise SettlementError('bills must close months in order from the '
                                  'earliest enable month')
        _verify_bill(bill, stores, receipts, agreement_by_key, supply, booked)
        for entry in bill['entries']:
            key = (entry['service_month'], entry['vendor'])
            booked[key] = booked.get(key, 0) + entry['amount']
    return {'version': 1, 'stores': stores, 'snapshots': snapshots,
            'agreements': agreements, 'receipts': receipts, 'bills': bills}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def init_books(stores_raw):
    """Create the books from the store registry: each store with the UTC
    date from which it counts. The enable-date midnight vector is fixed at
    all zero; counters never reset across months or snapshot installs."""
    fields(stores_raw, 'version stores')
    if type(stores_raw['version']) is not int or stores_raw['version'] != 1:
        raise SettlementError('unsupported stores file version')
    stores = [_store_row(row) for row in array(stores_raw['stores'], STORE_LIMIT)]
    store_ids = [store['store_id'] for store in stores]
    if not stores or len(set(store_ids)) != len(store_ids):
        raise SettlementError('stores must be nonempty with unique ids')
    stores = sorted(stores, key=lambda store: store['store_id'])
    return validate_books({'version': 1, 'stores': stores, 'snapshots': [],
                           'agreements': [], 'receipts': [], 'bills': []})


def import_batch(raw, batch_raw):
    """Fold one import batch into the books; the whole batch commits or
    nothing changes. A batch may register new v6 snapshots and new billing
    agreements alongside counter records. Re-registered snapshots replay by
    the content their snapshot_id binds (the query header does not
    participate); identical agreements replay, while a registered approval
    cannot change vendor or price. Records are validated against the merged
    registries, equivalent re-uploads add no fact, a repeated revision with
    different content rejects the whole batch, unseen lower revisions are
    kept as raw receipts without displacing the current one, and the merged
    current vectors must be non-decreasing per store along time."""
    books = validate_books(raw)
    fields(batch_raw, 'version snapshots agreements records')
    if type(batch_raw['version']) is not int or batch_raw['version'] != 1:
        raise SettlementError('unsupported batch version')
    snapshots = [validate_snapshot(row)
                 for row in array(batch_raw['snapshots'], SNAPSHOT_LIMIT)]
    agreements = [_agreement_row(row)
                  for row in array(batch_raw['agreements'], AGREEMENT_LIMIT)]
    records = [_record_row(row) for row in array(batch_raw['records'], RECORD_LIMIT)]

    report = {'snapshots_registered': 0, 'snapshots_replayed': 0,
              'agreements_registered': 0, 'agreements_replayed': 0,
              'receipts_added': 0, 'records_replayed': 0}
    known_snapshots = {snapshot['snapshot_id']: snapshot
                       for snapshot in books['snapshots']}
    new_snapshots = {}
    for snapshot in snapshots:
        snapshot_id = snapshot['snapshot_id']
        if snapshot_id in known_snapshots or snapshot_id in new_snapshots:
            report['snapshots_replayed'] += 1
            continue
        new_snapshots[snapshot_id] = snapshot
    known_agreements = {_agreement_key(agreement): agreement
                        for agreement in books['agreements']}
    new_agreements = {}
    for agreement in agreements:
        key = _agreement_key(agreement)
        existing = known_agreements.get(key, new_agreements.get(key))
        if existing is not None:
            if existing != agreement:
                raise SettlementError(
                    'approval already registered with different terms: %s#%d'
                    % key)
            report['agreements_replayed'] += 1
            continue
        new_agreements[key] = agreement

    supply = _supply_index(list(known_snapshots.values())
                           + list(new_snapshots.values()))
    all_agreements = {**known_agreements, **new_agreements}
    store_by_id = {store['store_id']: store for store in books['stores']}
    for record in records:
        store = store_by_id.get(record['store_id'])
        if store is None:
            raise SettlementError('record for an unregistered store: '
                                  + record['store_id'])
        if record['date'] <= store['enabled']:
            raise SettlementError('record date must be after the store enable '
                                  'date: %s at %s'
                                  % (record['store_id'], record['date']))
        for dim in record['counts']:
            _check_dimension(supply, all_agreements, dim)

    accepted = {}
    for receipt in books['receipts']:
        accepted[(receipt['store_id'], receipt['date'], receipt['revision'])] = \
            receipt['counts']
    fresh = []
    for record in records:
        identity = (record['store_id'], record['date'], record['revision'])
        prior = accepted.get(identity)
        if prior is not None:
            if prior != record['counts']:
                raise SettlementError(
                    'revision %d of %s at %s conflicts with the accepted '
                    'content' % (record['revision'], record['store_id'],
                                 record['date']))
            report['records_replayed'] += 1
            continue
        accepted[identity] = record['counts']
        fresh.append(record)

    if not new_snapshots and not new_agreements and not fresh:
        return books, report, False
    receipts = list(books['receipts'])
    for record in fresh:
        receipts.append({'seq': len(receipts) + 1,
                         'store_id': record['store_id'],
                         'date': record['date'],
                         'revision': record['revision'],
                         'counts': record['counts']})
    updated = {'version': 1, 'stores': books['stores'],
               'snapshots': sorted(list(known_snapshots.values())
                                   + list(new_snapshots.values()),
                                   key=lambda row: row['snapshot_id']),
               'agreements': sorted(all_agreements.values(),
                                    key=_agreement_key),
               'receipts': receipts, 'bills': books['bills']}
    updated = validate_books(updated)
    report['snapshots_registered'] = len(new_snapshots)
    report['agreements_registered'] = len(new_agreements)
    report['receipts_added'] = len(fresh)
    return updated, report, True


def reconcile_month(raw, month, store_id=None):
    """Real-time per-day reconciliation of one month: for every store the
    countable days (from its enable date on) are reported complete with the
    two revisions used, the positive per-dimension deltas and their supply
    and billing attribution, or missing with the endpoint that is absent.
    Always reflects the latest accepted revisions; never locked by closing."""
    books = validate_books(raw)
    month = _month(month)
    supply = _supply_index(books['snapshots'])
    agreements = {_agreement_key(agreement): agreement
                  for agreement in books['agreements']}
    stores = books['stores']
    if store_id is not None:
        store_id = text(store_id)
        stores = [store for store in stores if store['store_id'] == store_id]
        if not stores:
            raise SettlementError('unknown store: ' + store_id)
    report = {'month': month, 'stores': []}
    for store in stores:
        vectors = _current_vectors(store, books['receipts'])
        complete, missing = _store_days(store, vectors, month, supply, agreements)
        days = ([dict(day, status='complete') for day in complete]
                + [dict(day, status='missing') for day in missing])
        days.sort(key=lambda day: day['date'])
        report['stores'].append({'store_id': store['store_id'],
                                 'enabled': store['enabled'], 'days': days})
    return report


def close_month(raw, month):
    """Close one service month; returns (books, bill, changed).

    Months close in order from the earliest enable month. Re-closing an
    already closed month returns the original bill without recomputing or
    writing anything; skipping ahead fails. Closing with gaps is allowed:
    only complete days are recognized, and the bill freezes the receipt
    horizon, the covered days, the gaps and the pinned revisions it used."""
    books = validate_books(raw)
    month = _month(month)
    earliest = min(_month_of(store['enabled']) for store in books['stores'])
    index = _month_diff(earliest, month)
    if index < 0:
        raise SettlementError('month precedes the earliest enable month: ' + month)
    if index < len(books['bills']):
        return books, books['bills'][index], False
    if index > len(books['bills']):
        raise SettlementError('closing %s would skip unclosed service months'
                              % month)
    supply = _supply_index(books['snapshots'])
    agreements = {_agreement_key(agreement): agreement
                  for agreement in books['agreements']}
    basis = []
    for store in books['stores']:
        vectors = _current_vectors(store, books['receipts'])
        service = _month_of(store['enabled'])
        while _month_diff(service, month) >= 0:
            complete, missing = _store_days(store, vectors, service, supply,
                                            agreements)
            basis.append({'store_id': store['store_id'], 'month': service,
                          'complete_days': complete, 'missing_days': missing})
            service = _add_months(service, 1)
    entries = _entries(_recognized_from_basis(basis),
                       _booked_amounts(books['bills']), month)
    bill = {'month': month, 'seq': len(books['bills']) + 1,
            'receipts_through': len(books['receipts']), 'entries': entries,
            'vendor_totals': _vendor_totals(entries), 'basis': basis}
    books['bills'].append(bill)
    books = validate_books(books)
    return books, books['bills'][-1], True


def get_bill(raw, month):
    """Read back one closed bill exactly as it was frozen."""
    books = validate_books(raw)
    month = _month(month)
    for bill in books['bills']:
        if bill['month'] == month:
            return bill
    raise SettlementError('month is not closed: ' + month)

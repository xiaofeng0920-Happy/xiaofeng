"""Synthetic CSV order-line cleaning demo; Python standard library only."""
import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
import io
from pathlib import Path
import re
import unicodedata

FIELDS = ['order_id', 'line_id', 'date', 'sku', 'quantity', 'line_amount', 'currency', 'status']
MAX_ROWS = 10_000
MAX_FILE_BYTES = 10_000_000
MAX_FIELD = 4096


def check_quotes(text):
    state = 'start'
    for char in text:
        if state == 'quoted':
            if char == '"':
                state = 'closed'
        elif state == 'closed':
            if char == '"':
                state = 'quoted'
            elif char in ',\r\n':
                state = 'start'
            else:
                raise ValueError('malformed CSV quoting after closing quote')
        elif char == '"':
            if state != 'start':
                raise ValueError('malformed CSV quote inside unquoted field')
            state = 'quoted'
        elif char in ',\r\n':
            state = 'start'
        else:
            state = 'unquoted'
    if state == 'quoted':
        raise ValueError('unterminated CSV quoted field')


def escape(value):
    """Prefix spreadsheet-active text with an apostrophe, preserving original text."""
    text = str(value)
    offset = 0
    while offset < len(text) and (text[offset].isspace()
                                 or unicodedata.category(text[offset]) in ('Cc', 'Cf')):
        offset += 1
    prefix = text[offset:]
    return "'" + text if prefix.startswith(('=', '+', '-', '@')) else text


def validate(row):
    errors = []
    for field, value in zip(FIELDS, row):
        if not value:
            errors.append(field + ': required')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row[2]):
        errors.append('date: ISO YYYY-MM-DD required')
    else:
        try:
            date.fromisoformat(row[2])
        except ValueError:
            errors.append('date: impossible date')
    if not re.fullmatch(r'[0-9]+', row[4]):
        errors.append('quantity: non-negative integer required')
    try:
        amount = Decimal(row[5])
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            errors.append('line_amount: finite non-negative amount with <=2 decimals required')
        elif not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', row[5]):
            errors.append('line_amount: plain decimal required')
    except InvalidOperation:
        errors.append('line_amount: decimal required')
    if not re.fullmatch(r'[A-Z]{3}', row[6]):
        errors.append('currency: three uppercase letters required')
    if row[7] not in ('paid', 'cancelled', 'refunded'):
        errors.append('status: unknown value')
    return '; '.join(errors)


def read_inputs(inputs):
    records = []
    previous_limit = csv.field_size_limit(MAX_FIELD)
    try:
        for index, path in enumerate(inputs, 1):
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('input exceeds 10 MB file limit')
            with path.open(encoding='utf-8-sig', newline='') as source:
                text = source.read(MAX_FILE_BYTES + 1)
                if len(text) > MAX_FILE_BYTES:
                    raise ValueError('input exceeds file limit')
                check_quotes(text)
                reader = csv.reader(io.StringIO(text, newline=''), strict=True)
                header = next(reader, None)
                if header != FIELDS or len(header) > 20:
                    raise ValueError('header must match required columns exactly')
                while True:
                    coordinate = reader.line_num + 1
                    original = next(reader, None)
                    if original is None:
                        break
                    if len(original) != len(FIELDS):
                        raise ValueError('malformed CSV record width')
                    normalized = tuple(value.strip() for value in original)
                    records.append(dict(index=index, name=path.name, coordinate=coordinate,
                                        original=original, normalized=normalized,
                                        error=validate(normalized)))
                    if len(records) > MAX_ROWS:
                        raise ValueError('input exceeds 10000 total records')
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError('cannot read valid UTF-8 CSV: ' + str(exc)) from exc
    finally:
        csv.field_size_limit(previous_limit)
    return records


def write_csv(path, header, rows):
    with path.open('w', encoding='utf-8', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerows([[escape(value) for value in row] for row in rows])


def run_pipeline(inputs: list[Path], output_dir: Path) -> dict[str, int]:
    inputs = [Path(path) for path in inputs]
    output_dir = Path(output_dir)
    if not inputs:
        raise ValueError('at least one input required')
    if output_dir.exists() or output_dir.resolve() in {p.resolve() for p in inputs}:
        raise ValueError('output path must be new and distinct from inputs')
    records = read_inputs(inputs)
    groups = {}
    for record in records:
        row = record['normalized']
        if row[0] and row[1]:
            groups.setdefault(row[:2], []).append(record)
    conflicts = {key for key, rows in groups.items()
                 if len({r['normalized'] for r in rows}) > 1}
    counts = dict.fromkeys(['input_rows', 'accepted_rows', 'duplicate_rows', 'conflict_rows',
                           'invalid_rows', 'excluded_status_rows'], 0)
    counts['input_rows'] = len(records)
    seen = set()
    accepted, audit, totals = [], [], {}
    # Large bounded fields can contain large numbers; preserve exact decimal sums.
    with localcontext() as context:
        context.prec = MAX_FIELD + 20
        for record in records:
            row = record['normalized']
            key = row[:2]
            if key in conflicts:
                disposition, reason = 'conflict', 'key has differing normalized payloads'
            elif all(key) and key in seen:
                disposition, reason = 'duplicate', 'identical normalized copy; first retained'
            elif record['error']:
                disposition, reason = 'invalid', record['error']
            elif row[7] != 'paid':
                disposition, reason = 'excluded_status', row[7] + ' excluded from paid sales'
            else:
                disposition, reason = 'accepted', 'valid paid line'
                accepted.append(row)
                group = (row[6], row[3])
                quantity, amount = totals.get(group, (0, Decimal(0)))
                totals[group] = (quantity + int(row[4]), amount + Decimal(row[5]))
            if all(key):
                seen.add(key)
            counts[disposition + '_rows'] += 1
            audit.append([record['index'], record['name'], record['coordinate'], disposition,
                          reason, *record['original']])
        summaries = [[currency, sku, quantity, format(amount, '.2f')]
                     for (currency, sku), (quantity, amount) in sorted(totals.items())]
    output_dir.mkdir(parents=True, exist_ok=False)
    write_csv(output_dir / 'cleaned.csv', FIELDS, accepted)
    write_csv(output_dir / 'summary.csv', ['currency', 'sku', 'quantity', 'line_amount'], summaries)
    write_csv(output_dir / 'audit.csv', ['input_index', 'input_name', 'csv_row', 'disposition', 'reason', *FIELDS], audit)
    (output_dir / 'summary.json').write_text(json.dumps(counts, indent=2) + '\n', encoding='utf-8')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', nargs='+', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(run_pipeline(args.input, args.output), indent=2))
    except ValueError as exc:
        parser.exit(2, 'error: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()

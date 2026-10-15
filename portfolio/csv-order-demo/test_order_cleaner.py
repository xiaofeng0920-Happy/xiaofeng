import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HEADER = 'order_id,line_id,date,sku,quantity,line_amount,currency,status\n'
BASE = ['0001', '1', '2026-10-01', 'CUP', '1', '0.10', 'CNY', 'paid']


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_rows(self, rows):
        from order_cleaner import run_pipeline
        source = self.root / 'input.csv'
        with source.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(HEADER.strip().split(','))
            writer.writerows(rows)
        return run_pipeline([source], self.root / 'out')

    def test_reference(self):
        from order_cleaner import run_pipeline
        samples = sorted((Path(__file__).parent / 'samples').glob('*.csv'))
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in samples]
        self.assertEqual(run_pipeline(samples, self.root / 'out'), dict(input_rows=12, accepted_rows=6, duplicate_rows=1, conflict_rows=2, invalid_rows=2, excluded_status_rows=1))
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in samples])
        with (self.root / 'out' / 'summary.csv').open(newline='') as f:
            groups = {(r['currency'], r['sku']): (r['quantity'], r['line_amount']) for r in csv.DictReader(f)}
        self.assertEqual(groups, {('CNY','CUP'):('5','145.00'), ('CNY','BAG'):('1','49.00'), ('CNY',"'=2+2"):('1','5.00'), ('CNY','PEN'):('1','0.00'), ('USD','CUP'):('1','10.00')})
        self.assertIn('0001,', (self.root / 'out' / 'cleaned.csv').read_text())

    def test_identical_and_invalid_conflict(self):
        bad = BASE.copy(); bad[4] = ''
        result = self.run_rows([BASE, BASE, bad])
        self.assertEqual(result['conflict_rows'], 3)
        self.assertEqual(result['invalid_rows'], 0)

    def test_identical_normalized(self):
        spaced = [' ' + v + ' ' for v in BASE]
        self.assertEqual(self.run_rows([BASE, spaced])['duplicate_rows'], 1)

    def test_validation(self):
        changes = [(4,''),(4,'NaN'),(4,'Infinity'),(4,'-1'),(4,'1.5'),(5,''),(5,'NaN'),(5,'Infinity'),(5,'-1'),(5,'0.001'),(2,'10/01/2026'),(2,'2026-02-30'),(2,'2026-1-01'),(6,'cny'),(6,'AB1'),(7,'unknown')]
        rows=[]
        for i,(col,value) in enumerate(changes):
            row=BASE.copy(); row[0]=str(i); row[col]=value; rows.append(row)
        self.assertEqual(self.run_rows(rows)['invalid_rows'], len(rows))

    def test_zero_status_decimal_and_formula(self):
        rows=[]
        for i,(amount,status,sku) in enumerate([('0','paid',' \t=2+2'),('0.10','paid','CUP'),('0.20','paid','CUP'),('1','cancelled','CUP'),('1','refunded','CUP')]):
            row=BASE.copy(); row[0]=str(i); row[5]=amount; row[7]=status; row[3]=sku; rows.append(row)
        result=self.run_rows(rows)
        self.assertEqual(result['accepted_rows'],3)
        self.assertEqual(result['excluded_status_rows'],2)
        self.assertIn('0.30', (self.root/'out'/'summary.csv').read_text())
        self.assertIn("' \t=2+2", (self.root/'out'/'audit.csv').read_text())

    def test_blank_keys_independent(self):
        a=BASE.copy(); a[0]=''; b=a.copy(); b[3]='BAG'
        self.assertEqual(self.run_rows([a,b])['invalid_rows'],2)

    def test_unicode_formula_prefixes_and_unchanged_input(self):
        prefixes = [prefix + marker for prefix in ['\u00a0', '\u2003', '\u0085', '\u200b']
                    for marker in '=+-@']
        rows = []
        for i, prefix in enumerate(prefixes):
            row = BASE.copy(); row[0] = str(i); row[3] = prefix + 'payload'; rows.append(row)
        self.run_rows(rows)
        source = self.root / 'input.csv'
        with source.open(newline='', encoding='utf-8') as f:
            self.assertEqual(list(csv.reader(f))[1:], rows)
        for filename in ['cleaned.csv', 'audit.csv']:
            with (self.root/'out'/filename).open(newline='', encoding='utf-8') as f:
                exported = list(csv.DictReader(f))
            self.assertTrue(all(r['sku'].startswith("'") for r in exported), exported)
        with (self.root/'out'/'audit.csv').open(newline='', encoding='utf-8') as f:
            self.assertEqual([r['sku'] for r in csv.DictReader(f)], ["'" + r[3] for r in rows])

    def test_fatal_csv_and_headers(self):
        from order_cleaner import run_pipeline
        for text in ['', '\n', HEADER.replace('sku','date'), HEADER.replace(',status',''), HEADER.strip()+',extra\n', HEADER+'a,b\n', HEADER+'"unterminated\n', HEADER+'ab"cd,1,2026-10-01,CUP,1,1,CNY,paid\n', HEADER+'"abc"x,1,2026-10-01,CUP,1,1,CNY,paid\n', HEADER+','.join(['a'*4097,*BASE[1:]])+'\n']:
            with self.subTest(text=text):
                source=self.root/'bad.csv'; source.write_text(text)
                with self.assertRaises(ValueError): run_pipeline([source],self.root/'out')
                self.assertFalse((self.root/'out').exists())

    def test_limits_and_existing_output(self):
        from order_cleaner import run_pipeline
        source=self.root/'many.csv'; source.write_text(HEADER+(','.join(BASE)+'\n')*10001)
        with self.assertRaises(ValueError): run_pipeline([source],self.root/'out')
        self.assertFalse((self.root/'out').exists())
        out=self.root/'out'; out.mkdir(); sentinel=out/'keep'; sentinel.write_text('keep')
        with self.assertRaises(ValueError): run_pipeline([source],out)
        self.assertEqual(sentinel.read_text(),'keep')
        with self.assertRaises(ValueError): run_pipeline([source],source)

    def test_header_only_bom_and_cli(self):
        from order_cleaner import run_pipeline
        source=self.root/'empty.csv'; source.write_text('\ufeff'+HEADER,encoding='utf-8')
        self.assertEqual(run_pipeline([source],self.root/'out')['input_rows'],0)
        script=Path(__file__).parent/'order_cleaner.py'
        result=subprocess.run([sys.executable,str(script),'--input',str(source),'--output',str(self.root/'cli')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        result=subprocess.run([sys.executable,str(script),'--input',str(source),'--output',str(self.root/'cli')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)


if __name__ == '__main__': unittest.main()

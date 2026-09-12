#!/usr/bin/env python3
"""Map EC RAM bytes to DSDT field names and to observed temperature movement.

No root needed: consumes the directory written by probe_ec.py. Parses AML for
``OperationRegion (…, EmbeddedControl, …)`` and the ``Field`` lists that
reference those regions, then reports EC bytes whose values move with the
known hwmon references. It reports candidates only; a byte is not a board
temperature until its DSDT name and its behaviour both support that reading.

Usage: python3 ec_map.py DIR            # full report
       python3 ec_map.py --aml FILE.aml # field map only
"""
import argparse
import json
import re
import statistics
import sys
from pathlib import Path

NAMESEG = re.compile(rb'[A-Z_][A-Z0-9_]{3}')
SPACES = {0: 'SystemMemory', 1: 'SystemIO', 2: 'PCI_Config', 3: 'EmbeddedControl', 4: 'SMBus',
          5: 'CMOS', 6: 'PCIBARTarget', 7: 'IPMI', 8: 'GeneralPurposeIO', 9: 'GenericSerialBus', 10: 'PCC'}


def pkg_length(b, i):
    """Return (length, bytes_consumed). Length counts the PkgLength bytes."""
    b0 = b[i]
    n = b0 >> 6
    if n == 0:
        return b0 & 0x3F, 1
    length = b0 & 0x0F
    for k in range(n):
        length |= b[i + 1 + k] << (4 + 8 * k)
    return length, 1 + n


def name_string(b, i):
    """Return (name, bytes_consumed) or (None, 0) when not a valid NameString."""
    j = i
    prefix = ''
    while j < len(b) and b[j] in (0x5C, 0x5E):
        prefix += chr(b[j])
        j += 1
    if j >= len(b):
        return None, 0
    if b[j] == 0x00:
        return prefix, j + 1 - i
    if b[j] == 0x2E:
        segs = [b[j + 1:j + 5], b[j + 5:j + 9]]
        j += 9
    elif b[j] == 0x2F:
        cnt = b[j + 1]
        segs = [b[j + 2 + 4 * k:j + 6 + 4 * k] for k in range(cnt)]
        j += 2 + 4 * cnt
    else:
        segs = [b[j:j + 4]]
        j += 4
    if not all(len(s) == 4 and NAMESEG.fullmatch(s) for s in segs):
        return None, 0
    return prefix + '.'.join(s.decode() for s in segs), j - i


def const_arg(b, i):
    """Decode an integer constant TermArg; return (value|None, consumed)."""
    op = b[i]
    if op == 0x00:
        return 0, 1
    if op == 0x01:
        return 1, 1
    if op == 0x0A:
        return b[i + 1], 2
    if op == 0x0B:
        return int.from_bytes(b[i + 1:i + 3], 'little'), 3
    if op == 0x0C:
        return int.from_bytes(b[i + 1:i + 5], 'little'), 5
    if op == 0xFF:
        return -1, 1  # Ones
    name, n = name_string(b, i)
    return (None, n) if name else (None, 1)


def ec_regions(b):
    """Find OperationRegion definitions; return {last_seg: (full_name, space, offset, length)}."""
    regions = {}
    i = 0
    while True:
        i = b.find(b'\x5B\x80', i)
        if i < 0:
            break
        name, n = name_string(b, i + 2)
        if name and n:
            j = i + 2 + n
            if j < len(b) and b[j] in SPACES:
                space = b[j]
                off, n1 = const_arg(b, j + 1)
                ln, n2 = const_arg(b, j + 1 + n1)
                regions.setdefault(name.split('.')[-1], []).append((name, space, off, ln))
        i += 2
    return regions


def field_lists(b, ec_names):
    """Parse Field definitions whose region is an EmbeddedControl region."""
    fields = []
    i = 0
    while True:
        i = b.find(b'\x5B\x81', i)
        if i < 0:
            break
        try:
            plen, pn = pkg_length(b, i + 2)
            region, rn = name_string(b, i + 2 + pn)
            if not region or region.split('.')[-1] not in ec_names:
                i += 2
                continue
            j = i + 2 + pn + rn
            end = i + 2 + plen
            flags = b[j]
            j += 1
            bit = 0
            while j < end:
                op = b[j]
                if op == 0x00:
                    ln, n = pkg_length(b, j + 1)
                    bit += ln
                    j += 1 + n
                elif op == 0x01:
                    j += 3
                elif op == 0x02:
                    raise ValueError('ConnectField unsupported')
                elif op == 0x03:
                    j += 4
                else:
                    seg = b[j:j + 4]
                    if not NAMESEG.fullmatch(seg):
                        raise ValueError(f'bad field name at {j}')
                    ln, n = pkg_length(b, j + 4)
                    fields.append({'region': region, 'name': seg.decode(), 'bit': bit, 'bits': ln,
                                   'byte': bit // 8, 'flags': flags})
                    bit += ln
                    j += 4 + n
        except (IndexError, ValueError) as e:
            fields.append({'region': '?', 'name': f'<parse error at 0x{i:x}: {e}>', 'bit': 0, 'bits': 0, 'byte': 0, 'flags': 0})
        i += 2
    return fields


def tmp_methods(b):
    """Locate _TMP methods and decode a leading `Return (constant)` body."""
    out = []
    i = 0
    while True:
        i = b.find(b'_TMP', i)
        if i < 0:
            break
        body = b[i + 4:i + 24]
        decoded = None
        # Method body starts after MethodFlags (1 byte): ReturnOp 0xA4 + integer const
        if len(body) > 2 and body[1] == 0xA4:
            val, _ = const_arg(body, 2)
            if val is not None:
                decoded = f'Return(0x{val:X}) = {val} deci-K = {(val - 2732) / 10:.1f} C'
        out.append({'offset': i, 'hex': body.hex(), 'decoded': decoded})
        i += 4
    return out


def analyse_aml(path):
    b = Path(path).read_bytes()
    regions = ec_regions(b)
    ec = {k: v for k, v in regions.items() if any(r[1] == 3 for r in v)}
    return {'file': str(path), 'ec_regions': ec, 'fields': field_lists(b, set(ec)) if ec else [],
            'tmp': tmp_methods(b), 'all_region_spaces': sorted({SPACES[r[1]] for v in regions.values() for r in v})}


def analyse_samples(path):
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get('ec') and not r.get('error')]
    if len(rows) < 2:
        return {'samples': len(rows), 'note': 'not enough EC samples'}
    rams = [bytes.fromhex(r['ec']) for r in rows]
    refs = {}
    for key in rows[0]['ref']:
        try:
            refs[key] = [int(r['ref'][key]) / 1000 for r in rows]
        except (TypeError, ValueError, KeyError):
            pass
    report = []
    for off in range(256):
        series = [ram[off] for ram in rams]
        if len(set(series)) == 1:
            continue
        lo, hi = min(series), max(series)
        corr = {}
        for key, ref in refs.items():
            if len(set(ref)) > 1:
                try:
                    corr[key] = round(statistics.correlation(series, ref), 2)
                except (statistics.StatisticsError, ValueError):
                    pass
        best = max(corr.items(), key=lambda kv: abs(kv[1]), default=(None, 0))
        report.append({'byte': off, 'min': lo, 'max': hi, 'first': series[0], 'last': series[-1],
                       'plausible_temp_c': 15 <= lo and hi <= 110, 'best_ref': best[0], 'corr': best[1]})
    constant_temp_like = [off for off in range(256)
                          if len({ram[off] for ram in rams}) == 1 and 15 <= rams[0][off] <= 110]
    return {'samples': len(rows), 'span': (rows[0]['t'], rows[-1]['t']),
            'ref_ranges': {k: (min(v), max(v)) for k, v in refs.items()},
            'moving_bytes': sorted(report, key=lambda r: -abs(r['corr'])),
            'constant_bytes_in_temp_range': constant_temp_like}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dir', nargs='?')
    ap.add_argument('--aml')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    result = {'aml': [], 'samples': None}
    amls = [Path(args.aml)] if args.aml else sorted(Path(args.dir).glob('*.aml')) if args.dir else []
    for p in amls:
        result['aml'].append(analyse_aml(p))
    if args.dir and (Path(args.dir) / 'ec_samples.jsonl').exists():
        result['samples'] = analyse_samples(Path(args.dir) / 'ec_samples.jsonl')
    if args.json:
        json.dump(result, sys.stdout, indent=1, ensure_ascii=False)
        return
    for a in result['aml']:
        print(f"== {a['file']} == region spaces: {a['all_region_spaces']}")
        for k, v in a['ec_regions'].items():
            for full, space, off, ln in v:
                print(f"  EC region {full}: offset={off} length={ln}")
        for f in a['fields']:
            if f['bits']:
                print(f"  {f['region']:>6} byte 0x{f['byte']:02X} bit {f['bit'] % 8} width {f['bits']:>3}  {f['name']}")
            else:
                print(f"  {f['name']}")
        for t in a['tmp']:
            print(f"  _TMP @0x{t['offset']:x}: {t['decoded'] or t['hex']}")
    s = result['samples']
    if s:
        print(f"\n== EC samples: {s.get('samples')} {s.get('span', '')}")
        for k, v in s.get('ref_ranges', {}).items():
            print(f"  ref {k}: {v[0]:.1f}..{v[1]:.1f} C")
        print("  moving bytes (byte, range, best correlated reference):")
        for r in s.get('moving_bytes', [])[:40]:
            flag = ' temp-range' if r['plausible_temp_c'] else ''
            print(f"    0x{r['byte']:02X}: {r['min']:>3}..{r['max']:<3} corr={r['corr']:+.2f} vs {r['best_ref']}{flag}")
        print(f"  constant bytes in 15..110: {['0x%02X' % o for o in s.get('constant_bytes_in_temp_range', [])]}")


if __name__ == '__main__':
    main()

"""mIHC analysis engine (no UI). Used by easy_app.py.

Input : QuPath cell-measurement CSV per sample (Image, Classification, Signed distance ..., Centroid X/Y ...)
Output: per-sample workbook (counts, Log2FC, Prism, interactions) + cohort workbook.

Conventions (same as the BP016 / BP018 / Exosome scripts):
  * a Classification like "CD8: GzmB" is split on ':' into tokens; a cell counts toward every marker whose tokens it contains
  * zones from signed distance to Tumor: Total | In-Tumor (<=0) | Peri-Tumor (0..peri] | TILs (<=peri)
  * Log2FC = log2((arm median + 1) / (reference arm median + 1)) on per-arm medians of explant counts
  * interaction = nearest-neighbour distance per cell, median per explant; arm value = median of explants
"""
import os, re
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule

IMG_RE = re.compile(r'(?:^|[_\s\-])(L[A-Z])(\d+)(?=[_\s.\-]|$)')   # ..._LA1_...  /  LA2_merged...
DYE_RE = re.compile(r'op\d{3}|FITC|DAPI|AF\d{3}|Cy\d|APC|TRITC|Opal', re.I)
ZONES = ['Total', 'In-Tumor', 'Peri-Tumor', 'TILs']


# ── discovery ────────────────────────────────────────────────────────────────
def _is_measurement_csv(p):
    try:
        h = pd.read_csv(p, nrows=0).columns
    except Exception:
        return False
    return 'Classification' in h and 'Image' in h


def find_samples(folder):
    """{sample_id: csv_path}. Sample = first sub-folder under `folder` (or the csv name if the csv sits directly in it).
    Largest valid measurement CSV wins when a sample has several."""
    folder = os.path.abspath(folder)
    best = {}
    for dp, dn, fn in os.walk(folder):
        rel = os.path.relpath(dp, folder)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        dn[:] = [d for d in dn if d.lower() != 'results'] if depth < 3 else []
        for f in fn:
            if not f.lower().endswith('.csv') or re.search(r'delaunay|cluster', os.path.join(rel, f), re.I):
                continue                      # derived cluster exports are not sample measurements
            p = os.path.join(dp, f)
            sid = os.path.splitext(f)[0] if rel == '.' else rel.split(os.sep)[0]
            if sid in best and os.path.getsize(p) <= os.path.getsize(best[sid]):
                continue
            if _is_measurement_csv(p):
                best[sid] = p
    return dict(sorted(best.items()))


# ── loading ──────────────────────────────────────────────────────────────────
def _cols(path):
    cols = list(pd.read_csv(path, nrows=0).columns)
    pick = lambda pred: next((c for c in cols if pred(c)), None)
    return {
        'sd': pick(lambda c: c.startswith('Signed distance to annotation with Tumor')),
        'td': pick(lambda c: c.startswith('Distance to annotation with Tumor')),
        'x': pick(lambda c: c.startswith('Centroid X')),
        'y': pick(lambda c: c.startswith('Centroid Y')),
        'circ': 'Nucleus: Circularity' if 'Nucleus: Circularity' in cols else None,
        'otype': 'Object type' if 'Object type' in cols else None,
    }


def load_cells(path, circ_min=0.7):
    """Classified cells with columns: Classification, arm (LA..), explant (int), sd, td, x, y (missing ones are NaN)."""
    c = _cols(path)
    use = ['Image', 'Classification'] + [v for v in c.values() if v]
    parts = []                                    # read in chunks so file size does not matter, only the classified cells are kept
    for ch in pd.read_csv(path, usecols=use, chunksize=500_000, low_memory=False):
        if c['otype']:
            ch = ch[ch[c['otype']] == 'Cell']
        if c['circ'] and circ_min > 0:
            ch = ch[pd.to_numeric(ch[c['circ']], errors='coerce') > circ_min]
        parts.append(ch.dropna(subset=['Classification']))
    df = pd.concat(parts, ignore_index=True)
    m = df['Image'].astype(str).str.extract(IMG_RE)
    df = df.assign(arm=m[0].values, explant=pd.to_numeric(m[1]).values).dropna(subset=['arm', 'explant'])
    df['explant'] = df['explant'].astype(int)
    for k in ('sd', 'td', 'x', 'y'):
        df[k] = pd.to_numeric(df[c[k]], errors='coerce') if c[k] else np.nan
    return df[['Classification', 'arm', 'explant', 'sd', 'td', 'x', 'y']].reset_index(drop=True)


def detect(path):
    """What the CSV contains, for the UI: arms, phenotype labels, which columns exist."""
    c = _cols(path)
    df = load_cells(path, circ_min=0)
    cls = df['Classification'].value_counts()
    tok = {}
    for label, n in cls.items():
        for t in _tokens(label):
            tok[t] = tok.get(t, 0) + n
    arms = df.groupby('arm').agg(images=('explant', 'nunique'), max_explant=('explant', 'max'))
    return {'arms': arms, 'tokens': pd.Series(tok).sort_values(ascending=False), 'labels': cls,
            'has_sd': bool(c['sd']), 'has_td': bool(c['td']), 'has_xy': bool(c['x'] and c['y'])}


def short_label(sample_id):
    """FBB1T260043T72P2 -> S43, FBR1K240259T72P2 -> S259 (the 4-digit number before the timepoint); otherwise the id itself."""
    m = re.search(r'(\d{4})T\d+P?\d*$', sample_id)
    return f'S{int(m.group(1))}' if m else sample_id


def _tokens(label):
    return [t.strip() for t in str(label).split(':') if t.strip()]


# ── marker / zone logic ──────────────────────────────────────────────────────
def marker_classes(labels, rename, markers):
    """markers: {name: [marker-name tokens that must all be present]} -> {name: set(raw Classification strings)}.
    `rename` maps a raw token to the marker name you want (identity if absent)."""
    out = {}
    for name, req in markers.items():
        req = set(req)
        out[name] = {l for l in labels if req <= {rename.get(t, t) for t in _tokens(l)}}
    return out


def zone_masks(df, peri=50.0):
    sd = df['sd']
    return {'Total': pd.Series(True, index=df.index), 'In-Tumor': sd <= 0,
            'Peri-Tumor': (sd > 0) & (sd <= peri), 'TILs': sd <= peri}


def compute_counts(df, classes, peri=50.0):
    """Rows = (arm, explant) images that exist; columns = (marker, zone). Explants without an image stay absent (blank)."""
    present = df.groupby(['arm', 'explant']).size().index
    zm = zone_masks(df, peri)
    zones = ZONES if df['sd'].notna().any() else ['Total']      # no spatial annotation in the CSV: counts only, no empty zones
    res = {}
    for name, ok in classes.items():
        mk = df['Classification'].isin(ok)
        for z in zones:
            res[(name, z)] = df[mk & zm[z]].groupby(['arm', 'explant']).size().reindex(present, fill_value=0)
    out = pd.DataFrame(res)
    out.columns = pd.MultiIndex.from_tuples(out.columns, names=['marker', 'zone'])
    return out


def _nn_dist(fa, fb, exclude_self=True):
    """Distance from every cell in fa to the nearest cell in fb (NaN if none). fa/fb share an index, so a cell present in
    both is recognised and, with exclude_self, skipped as its own neighbour."""
    if fa.empty or fb.empty or fa[['x', 'y']].isna().any().any() or fb[['x', 'y']].isna().any().any():
        return np.full(len(fa), np.nan)
    k = min(2, len(fb)) if exclude_self else 1
    d, i = cKDTree(fb[['x', 'y']].to_numpy()).query(fa[['x', 'y']].to_numpy(), k=k)
    d, i = d.reshape(len(fa), -1), i.reshape(len(fa), -1)
    if not exclude_self:
        return d[:, 0]
    selfpos = pd.Series(np.arange(len(fb)), index=fb.index).reindex(fa.index).to_numpy()
    second = d[:, 1] if k > 1 else np.full(len(fa), np.nan)
    return np.where(i[:, 0] == selfpos, second, d[:, 0])


def compute_interactions(df, classes, pairs, exclude_self=True):
    """pairs: [(from_marker, to_marker | 'Tumor')]. Value per (arm, explant) = median over from-cells of the distance
    to the nearest to-cell (with exclude_self a cell is never its own neighbour) or, for 'Tumor', of the distance to the
    tumour annotation. exclude_self=False reproduces the old scripts, where a double-positive cell found itself at 0 um."""
    idx = df.groupby(['arm', 'explant']).size().index
    cols = [f'{a} to {b} [{s}]' for a, b in pairs for s in ('Mean', 'Median')]
    out = pd.DataFrame(np.nan, index=idx, columns=cols)
    for key, g in df.groupby(['arm', 'explant']):
        for a, b in pairs:
            fa = g[g['Classification'].isin(classes[a])]
            if fa.empty:
                continue
            dist = fa['td'].to_numpy() if b == 'Tumor' else _nn_dist(fa, g[g['Classification'].isin(classes[b])], exclude_self)
            if np.isfinite(dist).any():
                out.loc[key, f'{a} to {b} [Mean]'] = np.nanmean(dist)
                out.loc[key, f'{a} to {b} [Median]'] = np.nanmedian(dist)
    return out


def compute_proximity(df, classes, pairs, thr=20.0, peri=50.0, exclude_self=True):
    """For each zone and (source, neighbour) pair: how many source cells have a neighbour cell within `thr` um, and what % of
    the source cells that is. Source and neighbour cells are both taken from the same zone (as in the BP016/BP018 scripts).
    Returns (counts, percentages): rows = (arm, explant), columns = (pair label, zone)."""
    idx = df.groupby(['arm', 'explant']).size().index
    cols = pd.MultiIndex.from_tuples([(f'{a} with {b} (<={thr:g}µm)', z) for a, b in pairs for z in ZONES[1:]])
    cnt = pd.DataFrame(np.nan, index=idx, columns=cols)
    pct = pd.DataFrame(np.nan, index=idx, columns=cols)
    for key, g in df.groupby(['arm', 'explant']):
        g = g.dropna(subset=['x', 'y', 'sd'])
        for z, zm in zone_masks(g, peri).items():
            if z == 'Total':
                continue
            gz = g[zm]
            for a, b in pairs:
                col = (f'{a} with {b} (<={thr:g}µm)', z)
                src = gz[gz['Classification'].isin(classes[a])]
                if src.empty:
                    cnt.loc[key, col] = 0
                    continue
                dist = _nn_dist(src, gz[gz['Classification'].isin(classes[b])], exclude_self)
                n = int(np.sum(dist <= thr))     # NaN (no neighbour cells) compares False -> 0
                cnt.loc[key, col] = n
                pct.loc[key, col] = 100.0 * n / len(src)
    return cnt, pct


def arm_median(table):
    """Median of explants per arm (blank explants ignored)."""
    return table.groupby(level='arm').median()


def log2fc(med, ref):
    """med: rows = arms, columns = metrics -> DataFrame of Log2FC for non-reference arms."""
    others = [a for a in med.index if a != ref]
    return np.log2((med.loc[others] + 1) / (med.loc[[ref]].values + 1))


# ── Excel helpers ────────────────────────────────────────────────────────────
_thin = Side(style='thin')
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
ARM_FILLS = ['1F4E79', '375623', '843C0C', '4B0082', '006400', '7B3F00', '5C4033']
SOFT_FILLS = ['D6E4F0', 'E2EFDA', 'FCE4D6', 'E4DFEC', 'DDEBF7', 'FFF2CC', 'EAD1DC']


def _fill(c):
    return PatternFill(start_color=c, end_color=c, fill_type='solid')


def _cell(ws, r, c, v=None, bold=False, color='000000', fill=None, size=10, border=True, nf=None):
    if isinstance(v, (float, np.floating)):
        v = None if np.isnan(v) else round(float(v), 4)
    elif isinstance(v, np.integer):
        v = int(v)
    cell = ws.cell(r, c, v)
    if nf:
        cell.number_format = nf
    cell.font = Font(name='Calibri', bold=bold, color=color, size=size)
    cell.alignment = CENTER
    if fill:
        cell.fill = _fill(fill)
    if border:
        cell.border = BORDER
    return cell


def _title(ws, text, ncols):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(ncols, 2))
    _cell(ws, 1, 1, text, True, 'FFFFFF', '2F2F2F', 12)
    ws.row_dimensions[1].height = 22


def _widths(ws, first=14, rest=11):
    ws.column_dimensions['A'].width = first
    for i in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(i)].width = rest


def write_blocks(ws, blocks, title, color_scale=False, cohort_row=None, start_row=1):
    """Stacked tables: each block = (heading, DataFrame). Optional last row with the median of every column."""
    ncols = max(len(df.columns) for _, df in blocks) + 1
    _title(ws, title, ncols)
    r = start_row + 2
    for heading, df in blocks:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(df.columns) + 1)
        _cell(ws, r, 1, heading, True, 'FFFFFF', '1F3864', 11)
        r += 1
        _cell(ws, r, 1, df.index.name or '', True, 'FFFFFF', '404040')
        for j, col in enumerate(df.columns, 2):
            _cell(ws, r, j, str(col), True, 'FFFFFF', ARM_FILLS[(j - 2) % len(ARM_FILLS)])
        first = r + 1
        for lab, row in df.iterrows():
            r += 1
            _cell(ws, r, 1, str(lab), True, fill='F2F2F2')
            for j, v in enumerate(row.to_numpy(), 2):
                _cell(ws, r, j, v)
        last = r
        if cohort_row:
            r += 1
            _cell(ws, r, 1, cohort_row, True, fill='C6EFCE')
            for j, col in enumerate(df.columns, 2):
                _cell(ws, r, j, df[col].astype(float).median(), True, fill='C6EFCE')
        if color_scale and len(df):
            ws.conditional_formatting.add(
                f'B{first}:{get_column_letter(len(df.columns) + 1)}{last}',
                ColorScaleRule(start_type='num', start_value=-2, start_color='F8696B',
                               mid_type='num', mid_value=0, mid_color='FFEB84',
                               end_type='num', end_value=2, end_color='63BE7B'))
        r += 2
    ws.column_dimensions['A'].width = 22
    for j in range(2, ncols + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14


def _metric_label(m, z):
    return f'{m} - {z}'


# ── per-sample workbook ──────────────────────────────────────────────────────
def _grid(ws, title, table, arms, n_exp, sub_width=9, row_labels=None):
    """Explants as rows, arms side by side; table columns are (group, sub) e.g. (marker, zone). Mean / Median / Sum underneath."""
    groups = list(dict.fromkeys(table.columns.get_level_values(0)))
    ncol = len(table.columns)
    med, mean, tot = arm_median(table), table.groupby(level='arm').mean(), table.groupby(level='arm').sum(min_count=1)
    _title(ws, title, 1 + ncol * len(arms))
    for ai, arm in enumerate(arms):
        c0 = 2 + ai * ncol
        ws.merge_cells(start_row=2, start_column=c0, end_row=2, end_column=c0 + ncol - 1)
        _cell(ws, 2, c0, arm, True, 'FFFFFF', ARM_FILLS[ai % len(ARM_FILLS)], 12)
        for ci, col in enumerate(table.columns):
            gi = groups.index(col[0])
            _cell(ws, 4, c0 + ci, col[1], True, fill=SOFT_FILLS[gi % len(SOFT_FILLS)], size=9)
            if ci == 0 or table.columns[ci - 1][0] != col[0]:
                width = sum(1 for c in table.columns if c[0] == col[0])
                ws.merge_cells(start_row=3, start_column=c0 + ci, end_row=3, end_column=c0 + ci + width - 1)
                _cell(ws, 3, c0 + ci, col[0], True, fill=SOFT_FILLS[gi % len(SOFT_FILLS)])
            for e in range(1, n_exp + 1):
                _cell(ws, 4 + e, c0 + ci, table[col].get((arm, e), np.nan))
        for k, (tab, fillc) in enumerate([(mean, 'BDD7EE'), (med, 'C6EFCE'), (tot, 'FFE699')]):
            for ci, col in enumerate(table.columns):
                _cell(ws, 5 + n_exp + k, c0 + ci, tab.loc[arm, col] if arm in tab.index else np.nan, True, fill=fillc)
    _cell(ws, 3, 1, '', fill='2F2F2F'); _cell(ws, 4, 1, 'Explant', True, 'FFFFFF', '2F2F2F')
    for e in range(1, n_exp + 1):
        _cell(ws, 4 + e, 1, row_labels[e - 1] if row_labels else f'Explant {e}', True, fill='F2F2F2')
    for k, lab in enumerate(['Mean', 'Median', 'Sum']):
        _cell(ws, 5 + n_exp + k, 1, lab, True, fill='F2F2F2')
    ws.column_dimensions['A'].width = 24 if row_labels else 12
    for j in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(j)].width = sub_width
    ws.freeze_panes = 'B5'


def write_sample_workbook(path, sample, counts, inter, arm_names, ref, n_exp, info_note='', prox=None):
    """prox: optional (counts, percentages) from compute_proximity."""
    wb = Workbook()
    arms = [a for a in arm_names if a in counts.index.get_level_values(0)]
    med = arm_median(counts)

    # 1 Counts: explants as rows, arms side by side
    ws = wb.active
    ws.title = 'Counts'
    _grid(ws, f'mIHC cell counts | {sample} | explants as rows, arms side by side', counts, arms, n_exp, 8)

    # 2 Log2FC
    ws = wb.create_sheet('Log2FC')
    others = [a for a in arms if a != ref]
    heads = ['Marker', 'Zone'] + [f'{a} median' for a in arms] + [f'Log2FC {a} vs {ref}' for a in others]
    _title(ws, f'Log2FC vs {ref} | log2((arm median + 1) / ({ref} median + 1)) | {sample}', len(heads))
    for j, h in enumerate(heads, 1):
        _cell(ws, 3, j, h, True, 'FFFFFF', '404040')
    lfc = log2fc(med[counts.columns].loc[arms], ref) if ref in arms else None
    for r, col in enumerate(counts.columns, 4):
        _cell(ws, r, 1, col[0], True, fill='F2F2F2'); _cell(ws, r, 2, col[1])
        for j, a in enumerate(arms, 3):
            _cell(ws, r, j, med.loc[a, col])
        for j, a in enumerate(others, 3 + len(arms)):
            _cell(ws, r, j, lfc.loc[a, col] if lfc is not None else None)
    if others:
        L = get_column_letter
        ws.conditional_formatting.add(f'{L(3 + len(arms))}4:{L(2 + len(arms) + len(others))}{3 + len(counts.columns)}',
                                      ColorScaleRule(start_type='num', start_value=-2, start_color='F8696B', mid_type='num',
                                                     mid_value=0, mid_color='FFEB84', end_type='num', end_value=2, end_color='63BE7B'))
    _widths(ws, 14, 16)

    # 3 Prism (counts): one block per metric, arms as columns, explants as rows
    blocks = []
    for col in counts.columns:
        df = pd.DataFrame({a: [counts[col].get((a, e), np.nan) for e in range(1, n_exp + 1)] for a in arms},
                          index=pd.Index([f'Explant {e}' for e in range(1, n_exp + 1)], name='Explant'))
        blocks.append((_metric_label(*col), df))
    write_blocks(wb.create_sheet('Prism Counts'), blocks, f'Prism export - explant counts | {sample}')

    # 4 Interactions
    if inter is not None and len(inter.columns):
        imed = arm_median(inter)
        ws = wb.create_sheet('Interactions')
        _title(ws, f'Cell-cell interactions | median nearest-neighbour distance (um) per explant | {sample}', 1 + len(inter.columns) * len(arms))
        for pi, pair in enumerate(inter.columns):
            c0 = 2 + pi * len(arms)
            ws.merge_cells(start_row=2, start_column=c0, end_row=2, end_column=c0 + len(arms) - 1)
            _cell(ws, 2, c0, pair, True, fill=SOFT_FILLS[pi % len(SOFT_FILLS)])
            for ai, a in enumerate(arms):
                _cell(ws, 3, c0 + ai, a, True, 'FFFFFF', ARM_FILLS[ai % len(ARM_FILLS)])
                for e in range(1, n_exp + 1):
                    _cell(ws, 3 + e, c0 + ai, inter[pair].get((a, e), np.nan))
                _cell(ws, 4 + n_exp, c0 + ai, imed.loc[a, pair], True, fill='C6EFCE')
        _cell(ws, 2, 1, '', fill='2F2F2F'); _cell(ws, 3, 1, 'Explant', True, 'FFFFFF', '2F2F2F')
        for e in range(1, n_exp + 1):
            _cell(ws, 3 + e, 1, f'Explant {e}', True, fill='F2F2F2')
        _cell(ws, 4 + n_exp, 1, 'Median of explants', True, fill='C6EFCE')
        _widths(ws, 18, 10)
        blocks = [(p, pd.DataFrame({a: [inter[p].get((a, e), np.nan) for e in range(1, n_exp + 1)] for a in arms},
                                   index=pd.Index([f'Explant {e}' for e in range(1, n_exp + 1)], name='Explant')))
                  for p in inter.columns]
        write_blocks(wb.create_sheet('Prism Interactions'), blocks, f'Prism export - interaction distance (um) | {sample}')

    if prox is not None and len(prox[0].columns):
        _grid(wb.create_sheet('Proximity (Count)'), f'Source cells with a neighbour within the threshold | count | {sample}', prox[0], arms, n_exp, 10)
        _grid(wb.create_sheet('Proximity (%)'), f'Source cells with a neighbour within the threshold | % of source cells | {sample}', prox[1], arms, n_exp, 10)
        for nm, tab, what in (('Prism Prox Count', prox[0], 'count'), ('Prism Prox %', prox[1], '%')):
            blocks = [(f'{c[0]} - {c[1]}', pd.DataFrame({a: [tab[c].get((a, e), np.nan) for e in range(1, n_exp + 1)] for a in arms},
                                                       index=pd.Index([f'Explant {e}' for e in range(1, n_exp + 1)], name='Explant')))
                      for c in tab.columns]
            write_blocks(wb.create_sheet(nm), blocks, f'Prism export - proximity {what} | {sample}')

    # Legend
    ws = wb.create_sheet('Legend')
    rows = [('Total', 'all positive cells')]
    if 'In-Tumor' in counts.columns.get_level_values(1):
        rows += [('In-Tumor', 'signed distance to Tumor <= 0 um'), ('Peri-Tumor', '0 < signed distance <= peri limit'),
                 ('TILs', 'In-Tumor + Peri-Tumor (signed distance <= peri limit)')]
    rows += [('Counting', 'a cell counts toward every marker whose tokens it contains (double positives are added to each marker)'),
            ('Log2FC', f'log2((arm median + 1) / ({ref} median + 1)), medians taken over explants'),
            ('Interaction', 'per cell: distance to nearest cell of the target marker (a cell is never its own neighbour); '
                            "'Tumor' = distance to the tumour annotation; median per explant, then median over explants"),
            ('Proximity', "cells of the source marker (inside one zone) that have a neighbour-marker cell from the same zone within the "
                          'threshold distance; reported as a count and as % of the source cells'),
            ('Blank cell', 'no image for that explant')]
    if info_note:
        rows.append(('Run settings', info_note))
    for r, (a, b) in enumerate(rows, 1):
        _cell(ws, r, 1, a, True, fill='F2F2F2'); _cell(ws, r, 2, b); ws.cell(r, 2).alignment = Alignment(wrap_text=True)
    ws.column_dimensions['A'].width = 16; ws.column_dimensions['B'].width = 110
    wb.save(path)
    return path


# ── cohort workbook (BP018 / Exosome style) ──────────────────────────────────
GREEN, NR_H, NR_C, R_H, R_C = '375623', 'C5504B', 'FFE5E5', '1F4E79', 'DDEEFF'
ZONE_FILL = 'D9E1F2'


def _sheet_name(s):
    return re.sub(r'[\[\]:*?/\\]', '-', s)[:31]


def _blocks(ws, blocks, nf='#,##0.0', cf=False, header=None, row_label='Sample', size=11):
    """Stacked tables like the BP018 cohort sheets: title bar / header (row label + columns) / one row per sample / blank row.
    header: None (arm colours), one hex colour, or a list of hex colours (one per column)."""
    r = 1
    for title, df in blocks:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=max(2, len(df.columns) + 1))
        _cell(ws, r, 1, title, True, 'FFFFFF', '2F2F2F', 11)
        ws.row_dimensions[r].height = 20
        r += 1
        _cell(ws, r, 1, row_label, True, 'FFFFFF', '2F2F2F')
        for j, col in enumerate(df.columns, 2):
            h = header[j - 2] if isinstance(header, list) else (header or ARM_FILLS[(j - 2) % len(ARM_FILLS)])
            _cell(ws, r, j, str(col), True, 'FFFFFF', h)
        first = r + 1
        for lab, row in df.iterrows():
            r += 1
            _cell(ws, r, 1, str(lab), True).alignment = Alignment(horizontal='left', vertical='center')
            for j, v in enumerate(row.to_numpy(), 2):
                _cell(ws, r, j, v, size=size, nf=nf)
        if cf and len(df):
            ws.conditional_formatting.add(
                f'B{first}:{get_column_letter(len(df.columns) + 1)}{r}',
                ColorScaleRule(start_type='num', start_value=-2, start_color='F8696B', mid_type='num', mid_value=0,
                               mid_color='FFEB84', end_type='num', end_value=2, end_color='63BE7B'))
        r += 2
    ws.column_dimensions['A'].width = 14
    for j in range(2, max(len(df.columns) for _, df in blocks) + 2):
        ws.column_dimensions[get_column_letter(j)].width = 13


def _sample_summary(wb, label, sample, counts, arms, ref, n_exp, codes):
    """'<label> Summary': counts grid (explants x arms) plus the Summary Statistics table (means, medians, Log2FC, difference)."""
    ws = wb.create_sheet(_sheet_name(f'{label} Summary'))
    rows = [f'Explant {e} (' + '/'.join(f'{codes.get(a, "")}{e}' for a in arms) + ')' for e in range(1, n_exp + 1)]
    _grid(ws, f'Sample {label} ({sample})', counts, arms, n_exp, 9, row_labels=rows)
    others = [a for a in arms if a != ref]
    sfx = lambda a: '' if len(others) == 1 else f' {a}'
    med, mean = arm_median(counts), counts.groupby(level='arm').mean()
    r = 7 + n_exp + 3
    heads = (['Population', 'Zone'] + [f'{a} Mean' for a in arms] + [f'Log2FC (Mean){sfx(a)}' for a in others]
             + [f'{a} Median' for a in arms] + [f'Log2FC (Median){sfx(a)}' for a in others]
             + [f'Diff (Median {a}-{ref})' for a in others])
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(heads))
    _cell(ws, r, 1, 'Summary Statistics Table', True, 'FFFFFF', '2F2F2F', 11)
    r += 1
    for j, h in enumerate(heads, 1):
        _cell(ws, r, j, h, True, 'FFFFFF', '595959', 9)
    for (m, z) in counts.columns:
        r += 1
        vals = ([mean.loc[a, (m, z)] for a in arms] + [np.log2((mean.loc[a, (m, z)] + 1) / (mean.loc[ref, (m, z)] + 1)) for a in others]
                + [med.loc[a, (m, z)] for a in arms] + [np.log2((med.loc[a, (m, z)] + 1) / (med.loc[ref, (m, z)] + 1)) for a in others]
                + [med.loc[a, (m, z)] - med.loc[ref, (m, z)] for a in others])
        _cell(ws, r, 1, m, size=9).alignment = Alignment(horizontal='left', vertical='center')
        _cell(ws, r, 2, z, True, fill=ZONE_FILL, size=9)
        for j, v in enumerate(vals, 3):
            _cell(ws, r, j, v, size=10, nf='0.00')
    ws.column_dimensions['A'].width = 26
    return ws


def _rnr_prism(ws, title, groups, sub_cols, items, nf, label_fn):
    """Responder-vs-non-responder 'Prism' sheet: one label row + one value row per metric; sample columns, a gap between groups."""
    colour = {'NR': (NR_H, NR_C), 'R': (R_H, R_C)}
    width = len(sub_cols)
    ncols = sum(len(s) for _, s in groups) * width + (len(groups) - 1)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(2, ncols))
    _cell(ws, 1, 1, title, True, 'FFFFFF', '2F2F2F', 12, border=False)
    ws.row_dimensions[1].height = 22
    c = 1
    place = []                                           # (column, sample, group)
    for gi, (g, samples) in enumerate(groups):
        for s in samples:
            if width > 1:
                ws.merge_cells(start_row=2, start_column=c, end_row=2, end_column=c + width - 1)
            _cell(ws, 2, c, label_fn(s, g), True, 'FFFFFF', colour[g][0])
            for k, sc in enumerate(sub_cols):
                if width > 1:
                    _cell(ws, 3, c + k, sc, True, 'FFFFFF', colour[g][0], 9)
                place.append((c + k, s, g, sc))
            c += width
        c += 1                                           # gap column
    r = 3 if width == 1 else 4
    for label, df in items:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=max(2, ncols))
        _cell(ws, r, 1, label, True, 'FFFFFF', '595959').alignment = Alignment(horizontal='left', vertical='center')
        r += 1
        for col, s, g, sc in place:
            _cell(ws, r, col, df.loc[s, sc], fill=colour[g][1], size=11, nf=nf)
        r += 1
    for j in range(1, ncols + 1):
        ws.column_dimensions[get_column_letter(j)].width = 11


def _rnr_transposed(ws, title, groups, arms, items, nf):
    """Same data, metrics as rows and samples as columns, one block per arm (responders first)."""
    colour = {'NR': (NR_H, NR_C), 'R': (R_H, R_C)}
    samples = [(s, g) for g, ss in groups for s in ss]
    ncols = 2 + len(samples)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    _cell(ws, 1, 1, title, True, 'FFFFFF', '2F2F2F', 12, border=False)
    ws.row_dimensions[1].height = 22
    _cell(ws, 2, 1, None, fill='2F2F2F'); _cell(ws, 2, 2, None, fill='2F2F2F')
    c = 3
    for g, ss in groups:
        if not ss:
            continue
        ws.merge_cells(start_row=2, start_column=c, end_row=2, end_column=c + len(ss) - 1)
        _cell(ws, 2, c, 'Responders (R)' if g == 'R' else 'Non-Responders (NR)', True, 'FFFFFF', colour[g][0], 11)
        c += len(ss)
    _cell(ws, 3, 1, 'Arm', True, 'FFFFFF', '2F2F2F'); _cell(ws, 3, 2, 'Metric', True, 'FFFFFF', '2F2F2F')
    for j, (s, g) in enumerate(samples, 3):
        _cell(ws, 3, j, s, True, 'FFFFFF', colour[g][0])
    r = 4
    for a in arms:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        _cell(ws, r, 1, f'  {a}', True, 'FFFFFF', R_H, 12).alignment = Alignment(horizontal='left', vertical='center')
        r += 1
        for label, df in items:
            _cell(ws, r, 1, None); _cell(ws, r, 2, label, size=9).alignment = Alignment(horizontal='left', vertical='center')
            for j, (s, g) in enumerate(samples, 3):
                _cell(ws, r, j, df.loc[s, a], fill=colour[g][1], size=11, nf=nf)
            r += 1
    ws.column_dimensions['A'].width = 7
    ws.column_dimensions['B'].width = 38
    for j in range(3, ncols + 1):
        ws.column_dimensions[get_column_letter(j)].width = 11


def write_cohort_workbook(path, results, arm_names, ref, response=None, pct_pairs=None, labels=None, arm_codes=None):
    """results: {sample: {'counts': df, 'inter': df|None, 'prox': (count, pct)|None}}
    response: {sample: 'R'|'NR'} (optional); pct_pairs: [(numerator, denominator)] -> 'X % of Y' sheets;
    labels: {sample: short label such as 'S259'}; arm_codes: {arm name: image prefix such as 'LA'} for the explant labels."""
    samples = list(results)
    labels = {s: (labels or {}).get(s) or s for s in samples}
    codes = arm_codes or {}
    arms = [a for a in arm_names if any(a in r['counts'].index.get_level_values(0) for r in results.values())]
    others = [a for a in arms if a != ref]
    cols = results[samples[0]]['counts'].columns
    n_exp = int(max(r['counts'].index.get_level_values(1).max() for r in results.values()))
    idx = pd.Index([labels[s] for s in samples], name='Sample')
    mkr = lambda m: f'{m[0]} — {m[1]}'
    stat = lambda f, key: {s: f(results[s][key]).reindex(arms) for s in samples}
    amed, amean = (lambda t: arm_median(t)), (lambda t: t.groupby(level='arm').mean())
    meds, means = stat(amed, 'counts'), stat(amean, 'counts')
    has_inter = all(r.get('inter') is not None and len(r['inter'].columns) for r in results.values())
    has_prox = all(r.get('prox') is not None and len(r['prox'][0].columns) for r in results.values())
    imeds, imeans = (stat(amed, 'inter'), stat(amean, 'inter')) if has_inter else ({}, {})
    pcols = results[samples[0]]['prox'][0].columns if has_prox else []
    pmeds = {s: arm_median(results[s]['prox'][0]).reindex(arms) for s in samples} if has_prox else {}
    pmeans = {s: results[s]['prox'][0].groupby(level='arm').mean().reindex(arms) for s in samples} if has_prox else {}

    def per_metric(st, metrics, label, suffix):
        return [(f'{label(m)}  ({suffix})', pd.DataFrame({a: [st[s].loc[a, m] for s in samples] for a in arms}, index=idx))
                for m in metrics]

    def fc_blocks(st, metrics, label):
        return [(f'{label(m)}  (Log2FC vs {ref})', pd.DataFrame(
            {a: [np.log2((st[s].loc[a, m] + 1) / (st[s].loc[ref, m] + 1)) for s in samples] for a in others}, index=idx))
            for m in metrics]

    wb = Workbook()
    wb.remove(wb.active)
    add = lambda name: wb.create_sheet(_sheet_name(name))

    for s in samples:
        _sample_summary(wb, labels[s], s, results[s]['counts'], arms, ref, n_exp, codes)
    if others:
        _blocks(add('Log2FC'), fc_blocks(meds, cols, mkr), '0.00', cf=True, header=GREEN)
    _blocks(add('Median Counts'), per_metric(meds, cols, mkr, 'Median Counts'))
    _blocks(add('Mean Counts'), per_metric(means, cols, mkr, 'Mean Counts'), '#,##0.00')
    if has_inter:
        icols = list(results[samples[0]]['inter'].columns)
        dl = lambda c: f'{c} (µm)'
        if others:
            _blocks(add('Log2FC Distance'), fc_blocks(imeds, icols, dl), '0.00', cf=True, header=GREEN)
        _blocks(add('Median Distance'), per_metric(imeds, icols, dl, 'Median Distance'), '#,##0.00')
        _blocks(add('Mean Distance'), per_metric(imeans, icols, dl, 'Mean Distance'), '#,##0.00')
    if has_prox:
        for k, what in ((0, 'count'), (1, '%')):
            pst = {s: arm_median(results[s]['prox'][k]).reindex(arms) for s in samples}
            _blocks(add(f'Median Proximity ({what})'), per_metric(pst, pcols, mkr, 'Median Counts' if k == 0 else 'Median %'), '#,##0.00')
            if others and k == 0:
                _blocks(add('Log2FC Proximity (count)'), fc_blocks(pst, pcols, mkr), '0.00', cf=True, header=GREEN)

    # X as % of Y (median of explants, and sum of explants)
    markers = list(dict.fromkeys(cols.get_level_values(0)))
    zones = list(dict.fromkeys(cols.get_level_values(1)))
    for num, den in (pct_pairs or []):
        if num not in markers or den not in markers:
            continue
        for tag, f, what in (('', amed, 'Median'), (' (Sum)', lambda t: t.groupby(level='arm').sum(min_count=1), 'Sum')):
            ws = add(f'{num} % of {den}{tag}')
            st = stat(f, 'counts')
            width = len(zones)
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=1 + width * len(arms))
            _cell(ws, 1, 1, f'{num} as % of {den}  |  ({what} of explants ÷ {what} of explants) × 100', True, 'FFFFFF', '2F2F2F', 12, border=False)
            ws.row_dimensions[1].height = 22
            _cell(ws, 2, 1, 'Sample', True, 'FFFFFF', '595959'); _cell(ws, 3, 1, None, fill='2F2F2F')
            for ai, a in enumerate(arms):
                c0 = 2 + ai * width
                ws.merge_cells(start_row=2, start_column=c0, end_row=2, end_column=c0 + width - 1)
                _cell(ws, 2, c0, a, True, 'FFFFFF', ARM_FILLS[ai % len(ARM_FILLS)], 12)
                for zi, z in enumerate(zones):
                    _cell(ws, 3, c0 + zi, z, True, fill=ZONE_FILL, size=9)
            table = {s: [100 * st[s].loc[a, (num, z)] / st[s].loc[a, (den, z)] if st[s].loc[a, (den, z)] else np.nan
                         for a in arms for z in zones] for s in samples}
            for ri, s in enumerate(samples, 4):
                _cell(ws, ri, 1, f'{labels[s]} ({s})', True).alignment = Alignment(horizontal='left', vertical='center')
                for ci, v in enumerate(table[s], 2):
                    _cell(ws, ri, ci, v, fill=SOFT_FILLS[((ci - 2) // width) % len(SOFT_FILLS)], nf='0.00')
            ri = 4 + len(samples)
            _cell(ws, ri, 1, 'Cohort Median', True, '375623', 'C6EFCE')
            for ci in range(2, 2 + width * len(arms)):
                col = [v for v in (table[s][ci - 2] for s in samples) if v == v]
                _cell(ws, ri, ci, float(np.median(col)) if col else None, True, '375623', 'C6EFCE', nf='0.00')
            ws.column_dimensions['A'].width = 28
            for ci in range(2, 2 + width * len(arms)):
                ws.column_dimensions[get_column_letter(ci)].width = 14

    # Heatmap Prism: one sheet per arm, a zone x sample matrix per marker
    for a in arms:
        blocks = [(f'{m} — {a}  |  Median Count: Zone x Sample',
                   pd.DataFrame({labels[s]: [meds[s].loc[a, (m, z)] for z in zones] for s in samples}, index=pd.Index(zones, name='Zone')))
                  for m in markers]
        _blocks(add(f'Heatmap Prism ({a})'), blocks, '0.00', header=R_H, row_label='Zone', size=10)

    # Responders vs non-responders
    if response and {'R', 'NR'} <= set(response.get(s) for s in samples):
        grp = lambda g: [s for s in samples if response.get(s) == g]
        nr_first, r_first = [('NR', grp('NR')), ('R', grp('R'))], [('R', grp('R')), ('NR', grp('NR'))]
        lab = lambda s, g: f'{labels[s]} ({g})'
        frame = lambda st, m: pd.DataFrame({a: [st[s].loc[a, m] for s in samples] for a in arms}, index=samples)

        def items(cstat, dstat, pstat, fc=None):
            out = [(f'[Count]  {mkr(m)}', frame(cstat, m)) for m in cols]
            if has_prox:
                out += [(f'[Distance]  {mkr(c)}', frame(pstat, c)) for c in pcols]
            if has_inter:
                out += [(f'[Distance]  {c} (µm)', frame(dstat, c)) for c in icols]
            if fc:
                out = [(l, pd.DataFrame({fc: [np.log2((d.loc[s, fc] + 1) / (d.loc[s, ref] + 1)) for s in samples]}, index=samples)) for l, d in out]
            return out
        for a in others:
            nm = 'Log2FC R vs NR (Prism)' if len(others) == 1 else f'Log2FC R vs NR (Prism) {a}'
            _rnr_prism(add(nm), f'Log2FC ({a} vs {ref})  |  Responders vs Non-Responders  |  Left=NR  Right=R', nr_first, [a],
                       items(meds, imeds, pmeds, a), '0.00', lab)
        for stname, cst, dst, pst_ in (('Median', meds, imeds, pmeds), ('Mean', means, imeans, pmeans)):
            _rnr_prism(add(f'{stname} R vs NR (Prism)'), f'{stname} Counts  |  Responders vs Non-Responders  |  Left=NR  Right=R',
                       nr_first, arms, items(cst, dst, pst_), '#,##0.0', lab)
            tr = [(l, d.rename(index=labels)) for l, d in items(cst, dst, pst_)]
            _rnr_transposed(add(f'{stname} R vs NR (Transposed)'), f'{stname} Counts  |  Transposed  |  Responders left, Non-Responders right',
                            [(g, [labels[s] for s in ss]) for g, ss in r_first], arms, tr, '#,##0.0')

    # explant-level Prism export
    ex_head = []
    for s in samples:
        for a in arms:
            ex_head.append((response or {}).get(s) and (NR_H if response[s] == 'NR' else R_H) or ARM_FILLS[arms.index(a) % len(ARM_FILLS)])
    blocks = []
    for m in cols:
        d = {f'{labels[s]} ({a})': [results[s]['counts'][m].get((a, e), np.nan) for e in range(1, n_exp + 1)] for s in samples for a in arms}
        blocks.append((f'{mkr(m)} (Explant Counts)', pd.DataFrame(d, index=pd.Index([f'Expl_{e}' for e in range(1, n_exp + 1)], name='Explant'))))
    _blocks(add('Prism Export (Explant Counts)'), blocks, '#,##0.0', header=ex_head, row_label='Explant', size=9)
    wb.save(path)
    return path

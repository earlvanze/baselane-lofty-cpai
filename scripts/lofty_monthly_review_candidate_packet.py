#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,hashlib,json,os,re
from datetime import date,datetime,timedelta,timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from baselane_dao_cash_authority import (
    DEFAULT_REPORT as DEFAULT_DAO_CASH_REPORT,
    apply_to_summary as apply_dao_cash_to_summary,
    load_report as load_dao_cash_report,
)
from baselane_daily_source_cash_balance_audit import canonical_property_split_gls, load_cf_module, source_cash_property_aliases, source_cash_match_key
from canonical_property_ledger import DivergentCanonicalLedgerError, resolve_equivalent_ledgers
from coownership_reserve_policy import canonical_property as canonical_reserve_property
from coownership_reserve_policy import eco_gl_net_of_accruals, read_rows as read_reserve_rows
from baselane_reconciliation_policy import is_cash_basis_excluded_row
from lofty_property_paths import display_name_for_property_path, resolve_property_path as resolve_canonical_property_path
from lofty_monthly_exclusions import match_exclusion_guard, monthly_exclusion_guards

ST={"ohio":"oh","illinois":"il","new york":"ny","colorado":"co","florida":"fl","tennessee":"tn","pennsylvania":"pa","california":"ca","texas":"tx","north carolina":"nc","south carolina":"sc","new jersey":"nj","new mexico":"nm","west virginia":"wv","virginia":"va","georgia":"ga","indiana":"in","michigan":"mi","wisconsin":"wi","minnesota":"mn","missouri":"mo","kentucky":"ky","alabama":"al","arizona":"az","arkansas":"ar","connecticut":"ct","delaware":"de","iowa":"ia","kansas":"ks","louisiana":"la","maryland":"md","massachusetts":"ma","nevada":"nv","oklahoma":"ok","oregon":"or","utah":"ut","washington":"wa"}
PROPERTY_UPDATE_MARKER_RE=re.compile(r"(?m)^-\s+\*{0,2}Property Update\s*\(")
DATED_UPDATE_HEADING_RE=re.compile(r"(?m)^##\s+\d{4}-\d{2}-\d{2}\s*$")
PROPERTY_UPDATES_HEADER_RE=re.compile(r"(?m)^# Property Updates\s*$")
MAX_OWNER_UPDATE_CHARS=3500
REV={"rents","repairs_reimbursement","fees_other_revenue"}; OPEX={"cleaning_maintenance","insurance","legal_professional","property_mgmt_fee","software_subscriptions","repairs_supplies","taxes","utilities"}
LIMITED_UPDATE_LANGUAGE_RE=re.compile(
    r"(?is)\n*This month's update is limited to verified cash-position data from Lofty and ECO records\."
    r"\s*No tenant ledger rows are included\.\s*"
)
FINANCIAL_DETAIL_HEADING="Financial detail:"
CURRENT_REVIEWED_FINANCIAL_SUMMARY_SENTENCE="This month's update includes the current reviewed financial summary from the guarded monthly workflow."
DEFAULT_CASH_ADJUSTMENTS=Path(__file__).absolute().parents[1]/"config/property_cash_summary_adjustments.json"

def iso_z(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def parse_iso_date(v): return date.fromisoformat(v)
def read_json(p):
    return json.loads(p.read_text(encoding="utf-8")) if p and p.is_file() else None
def default_dropbox_root():
    for p in (Path('/mnt/c/Users/digit/Dropbox'),Path.home()/'Dropbox'):
        if p.exists(): return p
    return Path('/mnt/c/Users/digit/Dropbox')
def default_source_ledger(): return default_dropbox_root()/"Projects/assetrail/ECO Systems General Ledger.csv"
def default_lofty_properties_dir(): return Path(__file__).absolute().parents[1]/'lofty_properties'
def slugify(v): return re.sub(r'[^a-z0-9]+','-',str(v).lower()).strip('-') or 'property'
def norm(v):
    s=str(v or '').lower().replace('&',' and ')
    s=re.sub(r'\b(public|dao|llc)\b',' ',s)
    for full,abbr in ST.items(): s=re.sub(rf'\b{re.escape(full)}\b',abbr,s)
    s=re.sub(r'[^a-z0-9]+',' ',s); return re.sub(r'\s+',' ',s).strip()
def names_match(a,b):
    a,b=norm(a),norm(b); return bool(a and b and (a==b or a in b or b in a))
def parse_money(v):
    s='' if v is None else str(v).strip()
    if not s: return None
    neg=(s.startswith('(') and s.endswith(')')) or s.startswith('-')
    s=s.strip('()').replace('$','').replace(',','').strip()
    if s.startswith('-'): s=s[1:].strip()
    try: n=float(s)
    except ValueError: return None
    return -n if neg else n
def format_money(v):
    n=parse_money(v)
    if n is None: return 'Not available'
    return ('-' if n<0 else '')+f"${abs(n):,.2f}"

def property_cash_summary_adjustments(property_name):
    payload=read_json(DEFAULT_CASH_ADJUSTMENTS)
    if not isinstance(payload,dict): return {}
    for item in payload.get('properties') or []:
        if isinstance(item,dict) and names_match(property_name,item.get('property_name')):
            return item
    return {}
def cash_position_adjustment_rows(s):
    rows=[]
    for item in (s.get('property_cash_summary_adjustments') or {}).get('cash_position_lines') or []:
        if not isinstance(item,dict): continue
        rows.append(f"| {item.get('metric')} | {format_money(item.get('amount'))} | {item.get('source')} |")
    return rows
def source_evidence_adjustment_rows(s):
    rows=[]
    for item in (s.get('property_cash_summary_adjustments') or {}).get('source_evidence') or []:
        if not isinstance(item,dict): continue
        rows.append(f"| {item.get('field')} | {item.get('value')} |")
    return rows

def load_lofty_reserve_index(lofty_profile_json=None,lofty_properties_dir=None,lofty_all_properties_json=None):
    rows=[]
    def add(prop,source):
        if not isinstance(prop,dict): return
        addr=prop.get('address') or prop.get('fullAddress') or prop.get('name')
        if addr is None or 'curr_maintenance_reserve' not in prop: return
        reserve=parse_money(prop.get('curr_maintenance_reserve'))
        if reserve is None and prop.get('curr_maintenance_reserve')==0: reserve=0.0
        monthly_rent=parse_money(prop.get('monthly_rent') or prop.get('monthlyRent') or prop.get('currentRent'))
        annual_rent=parse_money(prop.get('annualRent') or prop.get('annual_rent'))
        if monthly_rent is None and annual_rent is not None: monthly_rent=round(annual_rent/12,2)
        if reserve is not None: rows.append({'address':str(addr),'curr_maintenance_reserve':reserve,'monthly_rent':monthly_rent,'annual_rent':annual_rent,'source':source,'source_mode':'pm_or_sdk_snapshot'})
    def add_nested(payload,source):
        if isinstance(payload,dict):
            add(payload,source)
            for value in payload.values(): add_nested(value,source)
        elif isinstance(payload,list):
            for value in payload: add_nested(value,source)
        elif isinstance(payload,str):
            text=payload.strip()
            if not text or text[0] not in '[{': return
            try: decoded=json.loads(text)
            except (TypeError,ValueError): return
            add_nested(decoded,source)
    payload=read_json(lofty_profile_json)
    if isinstance(payload,dict): add_nested(payload,str(lofty_profile_json))
    payload=read_json(lofty_all_properties_json)
    add_nested(payload,str(lofty_all_properties_json))
    if lofty_properties_dir and lofty_properties_dir.is_dir():
        for p in sorted(lofty_properties_dir.glob('*.json')):
            payload=read_json(p); add(payload.get('property') if isinstance(payload,dict) and isinstance(payload.get('property'),dict) else payload, str(p))
    return rows
def match_lofty_reserve(property_name,index):
    matches=[r for r in index if names_match(property_name,r.get('address'))]
    matches.sort(key=lambda r:len(norm(r.get('address'))),reverse=True)
    return matches[0] if matches else None

def financials_md_lofty_reserve(financials_md:Path):
    if not financials_md or not financials_md.is_file(): return None
    text=financials_md.read_text(encoding='utf-8',errors='replace')
    patterns=[
        r"(?mi)^\|\s*Lofty Operating Cash\s*\|\s*(?P<amount>[^|\n]+?)\s*\|\s*Lofty\s*`curr_maintenance_reserve`\s*\|",
        r"(?mi)^\s*-\s*Lofty-held current maintenance reserve:\s*(?P<amount>Not available|-?\$[\d,]+\.\d{2})\s*$",
        r"(?mi)^\s*-\s*\*\*Lofty-held current maintenance reserve:\*\*\s*(?P<amount>Not available|-?\$[\d,]+\.\d{2})\s*$",
        r"(?mi)^\s*-\s*\*\*Current Maintenance Reserve:\*\*\s*(?P<amount>Not available|-?\$[\d,]+\.\d{2})\s*$",
    ]
    for pattern in patterns:
        match=re.search(pattern,text)
        if not match: continue
        amount=parse_money(match.group('amount'))
        if amount is not None:
            return {'address':financials_md.parent.parent.parent.name if financials_md.parent else financials_md.stem,'curr_maintenance_reserve':amount,'source':str(financials_md),'source_mode':'financials_md'}
    return None

def financials_md_monthly_rent(financials_md:Path):
    if not financials_md or not financials_md.is_file(): return None
    text=financials_md.read_text(encoding='utf-8',errors='replace')
    patterns=[
        r"(?mi)^\|\s*(?:Monthly Gross Rent|Current Rent|Scheduled Monthly Rent)\s*\|\s*(?P<amount>[^|\n]+?)\s*\|",
        r"(?mi)^\s*-\s*\*\*(?:Current Rent|Monthly Gross Rent|Scheduled Monthly Rent):\*\*\s*(?P<amount>[^\n]+?)\s*$",
        r"(?mi)^\s*(?:Current Rent|Monthly Gross Rent|Scheduled Monthly Rent):\s*(?P<amount>[^\n]+?)\s*$",
    ]
    for pattern in patterns:
        match=re.search(pattern,text)
        if not match: continue
        amount=parse_money(match.group('amount'))
        if amount is not None and amount > 0:
            return {'monthly_rent':amount,'source':str(financials_md),'source_mode':'financials_md'}
    return None

def reserve_index_source_counts(index):
    counts={}
    for row in index:
        source=str(row.get('source') or 'unknown')
        counts[source]=counts.get(source,0)+1
    return dict(sorted(counts.items()))

def missing_reserve_artifact_paths(output_dir:Path):
    return {'missing_lofty_reserve_csv':output_dir/'missing-lofty-reserve-review.csv','missing_lofty_reserve_markdown':output_dir/'missing-lofty-reserve-review.md'}

def write_missing_reserve_artifacts(output_dir:Path,rows,run_month):
    paths=missing_reserve_artifact_paths(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    fields=['property_name','managed_name','input_property_name','financials_md','next_action']
    enriched=[]
    for row in rows:
        enriched.append({**row,'next_action':'Populate Lofty curr_maintenance_reserve from live Lofty listing/profile snapshot, then regenerate monthly review candidate packet.'})
    with paths['missing_lofty_reserve_csv'].open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader()
        for item in enriched: writer.writerow({field:item.get(field) or '' for field in fields})
    lines=['# Missing Lofty Reserve Review Queue','',f"- Run month: `{run_month or 'unknown'}`",f"- Missing reserve count: `{len(enriched)}`","- Required action: populate `curr_maintenance_reserve` from the live Lofty listing/profile snapshot, then regenerate the monthly review candidate packet.",'']
    for idx,item in enumerate(enriched,1):
        lines += [f"## {idx}. {item.get('property_name') or item.get('input_property_name') or 'Unknown property'}",'',f"- Managed name: `{item.get('managed_name') or ''}`",f"- Input property name: `{item.get('input_property_name') or ''}`",f"- FINANCIALS.md: `{item.get('financials_md') or ''}`",f"- Next action: {item.get('next_action')}",'']
    paths['missing_lofty_reserve_markdown'].write_text('\n'.join(lines).rstrip()+'\n',encoding='utf-8')
    return {key:str(value) for key,value in paths.items()}

def resolve_property_path(path:Path):
    return resolve_canonical_property_path(path)
def resolve_record_document_path(explicit,property_path:Path,subdir:str):
    ep=Path(str(explicit)) if str(explicit or '').strip() else None
    base=ep.name if ep else ('FINANCIALS.md' if 'P&L' in subdir else 'UPDATES.md')
    candidates=[property_path/'Public'/subdir/base, property_path/subdir/base]
    if base == 'FINANCIALS.md':
        candidates.extend([
            property_path/'Public'/'07 - P&L & Owner Statements'/base,
            property_path/'07 - P&L & Owner Statements'/base,
        ])
    for c in candidates:
        if c.is_file(): return c
    if ep and ep.is_file():
        if property_path.name.endswith(' Public'):
            return property_path/subdir/base
        return ep
    if property_path.name.endswith(' Public'):
        return property_path/subdir/base
    return property_path/'Public'/subdir/base

def source_ledger_zero_row_status(property_name,source_ledger):
    if not source_ledger or not source_ledger.is_file(): return None
    matched=0
    with source_ledger.open(newline='',encoding='utf-8-sig',errors='replace') as h:
        r=csv.DictReader(h); keys=[k for k in (r.fieldnames or []) if k and k.strip().lower() in {'property','address','property name'}]
        for row in r:
            vals=[row.get(k,'') for k in keys] or [' '.join(str(v or '') for v in row.values())]
            if any(names_match(property_name,v) for v in vals): matched+=1
    return {'status':'missing_property_split','amount':None,'path':None,'row_count':0} if matched else {
        'status':'ok','amount':0.0,'path':str(source_ledger),'row_count':0,
        'source_mode':'source_ledger_zero_rows','cash_basis_amount':0.0,
        'cash_basis_row_count':0,
        'cash_basis_scope':'property_split_rows_excluding_non_cash_closes_and_accrual_journals',
        'cash_basis_amount_as_of_month':0.0,'cash_basis_row_count_as_of_month':0,
    }
def row_in_run_month(row,run_month):
    if not run_month: return True
    raw=str(row[1] if len(row)>1 else '').strip()
    if raw.startswith(run_month): return True
    try:
        return datetime.strptime(raw,"%B %d, %Y").strftime("%Y-%m")==run_month
    except ValueError:
        return raw.lower() in {"date", ""}
def row_date(raw):
    raw=str(raw or '').strip()
    for fmt in ("%B %d, %Y","%b %d, %Y","%Y-%m-%d","%m/%d/%Y","%-m/%-d/%Y"):
        try:
            return datetime.strptime(raw,fmt).date()
        except ValueError:
            continue
    return None
def month_end(run_month):
    if not run_month: return None
    try:
        start=datetime.strptime(run_month+"-01","%Y-%m-%d").date()
    except ValueError:
        return None
    return date(start.year,12,31) if start.month==12 else date(start.year,start.month+1,1)-timedelta(days=1)
def included_through_reporting_cutoff(row_date_value,notes,cutoff,run_month):
    if row_date_value is None:
        return False
    if cutoff is None or row_date_value<=cutoff:
        return True
    return bool(
        run_month
        and row_date_value.strftime("%Y-%m")==run_month
        and str(notes or "").strip().upper().startswith("AOPS-")
    )

@lru_cache(maxsize=4)
def source_ledger_properties(source_ledger_path:str):
    """Return source property identities once per raw-ledger path."""
    source_ledger=Path(source_ledger_path)
    if not source_ledger.is_file(): return frozenset()
    cf=load_cf_module()
    return frozenset(
        row.get("_property")
        for row in cf.load_gl_data(source_ledger)
        if row.get("_property")
    )

def preferred_property_split_ledger(financials_md:Path,property_name,source_ledger=None):
    """Use the daily source-cash resolver so legacy folders cannot win by mtime."""
    if not source_ledger or not Path(source_ledger).is_file(): return None
    properties=source_ledger_properties(str(Path(source_ledger)))
    if not properties: return None
    cf=load_cf_module()
    aliases=source_cash_property_aliases(cf,property_name,set(properties))
    # Folder names can retain a parcel range ("326-332") while the ledger
    # identity uses only the primary street number ("326"). Include both
    # source identities and let the property-split evidence select the fuller
    # canonical export.
    def street_identity(value):
        key=source_cash_match_key(cf,value)
        abbreviations={"street":"st","avenue":"ave","road":"rd","drive":"dr","lane":"ln","circle":"cir"}
        key=re.sub(r"\b(street|avenue|road|drive|lane|circle)\b",lambda match:abbreviations[match.group(1)],key)
        # Keep both the primary address and ordinal street number. The shared
        # matcher normalizes `41st` to `41`; dropping all numeric tokens would
        # incorrectly collapse 3178 W 41st and 1456 W 85th to `w st`.
        address_match=re.match(r"\s*(\d+)(?:\s*-\s*\d+)?\b",str(value))
        primary_address=address_match.group(1) if address_match else None
        tokens=key.split()
        if primary_address and tokens and tokens[0].isdigit():
            tokens=tokens[1:]
        return " ".join(([primary_address] if primary_address else [])+tokens)
    expected=street_identity(property_name)
    aliases=sorted(set(aliases)|{candidate for candidate in properties if expected and street_identity(candidate)==expected})
    if not aliases: return None
    candidates=[
        path
        for alias in aliases
        for path in canonical_property_split_gls(financials_md,alias,set(properties),cf)
    ]
    # Never let a sibling directory's ledger stand in for this property. The
    # shared resolver may search a state-wide Public tree; its resulting file
    # must still carry this property's address identity.
    candidates=[
        path
        for path in candidates
        if (
            (ledger_identity:=street_identity(re.sub(r"^ECO Systems General Ledger - ","",path.stem,flags=re.IGNORECASE)))
            and (ledger_identity == expected or ledger_identity.startswith(expected+" "))
        )
    ]
    if not candidates: return None
    return resolve_equivalent_ledgers(candidates)

def gl_column_e_sum(financials_md:Path,property_name,source_ledger=None,run_month=None,cutoff_date=None):
    search_dirs=[financials_md.parent]
    owner_statement_dir=financials_md.parent.parent/'07 - P&L & Owner Statements'
    if owner_statement_dir not in search_dirs:
        search_dirs.append(owner_statement_dir)
    c=[p for d in search_dirs for p in d.glob('*General Ledger*.csv') if p.is_file() and p.name.lower()!='gl rows.csv']
    try:
        preferred=preferred_property_split_ledger(financials_md,property_name,source_ledger)
    except DivergentCanonicalLedgerError as exc:
        return {
            'status':'ambiguous_canonical_source',
            'amount':None,
            'path':None,
            'row_count':0,
            'collision_sources':[str(path) for path in exc.paths],
        }
    if preferred:
        c=[preferred]
    property_key=norm(property_name); property_key=property_key[:-7].strip() if property_key.endswith(' public') else property_key
    exact_name=norm(f'ECO Systems General Ledger - {property_key}.csv')
    c.sort(key=lambda p:(norm(p.name)==exact_name,p.stat().st_mtime),reverse=True)
    if not c: return source_ledger_zero_row_status(property_name,source_ledger) or {'status':'missing','amount':None,'path':None,'row_count':0}
    total=0.0; count=0; cutoff_total=0.0; cutoff_count=0; cash_total=0.0; cash_count=0; cutoff_cash_total=0.0; cutoff_cash_count=0; cutoff=month_end(run_month)
    if cutoff_date is not None and (cutoff is None or cutoff_date < cutoff):
        cutoff=cutoff_date
    with c[0].open(newline='',encoding='utf-8-sig',errors='replace') as h:
        reader=csv.reader(h); header=next(reader,[]); notes_index=next((i for i,v in enumerate(header) if str(v).strip().lower()=='notes'),None)
        header_indexes={str(v).strip().lower(): i for i,v in enumerate(header) if str(v).strip()}
        amount_index=next((header_indexes.get(key) for key in ('amount','eco gl column e','column e') if header_indexes.get(key) is not None),4)
        date_index=header_indexes.get('date',1)
        for row in reader:
            if len(row)<=amount_index: continue
            a=parse_money(row[amount_index])
            if a is not None:
                total+=a; count+=1
                d=row_date(row[date_index] if len(row)>date_index else "")
                ledger_row={'Notes':row[notes_index] if notes_index is not None and notes_index<len(row) else ''}
                included=included_through_reporting_cutoff(d,ledger_row['Notes'],cutoff,run_month)
                if included: cutoff_total+=a; cutoff_count+=1
                if not is_cash_basis_excluded_row(ledger_row):
                    cash_total+=a; cash_count+=1
                    if included: cutoff_cash_total+=a; cutoff_cash_count+=1
    bounded=cutoff_date is not None
    return {'status':'ok','amount':round(cutoff_total if bounded else total,2),'path':str(c[0]),'row_count':cutoff_count if bounded else count,'scope':'property_split_rows_through_reporting_cutoff' if bounded else 'all_property_split_rows','amount_all_rows':round(total,2),'row_count_all_rows':count,'amount_as_of_month':round(cutoff_total,2),'row_count_as_of_month':cutoff_count,'cash_basis_amount':round(cutoff_cash_total if bounded else cash_total,2),'cash_basis_row_count':cutoff_cash_count if bounded else cash_count,'cash_basis_amount_all_rows':round(cash_total,2),'cash_basis_row_count_all_rows':cash_count,'cash_basis_scope':'property_split_rows_excluding_non_cash_closes_and_accrual_journals_through_reporting_cutoff' if bounded else 'all_property_split_rows_excluding_non_cash_closes_and_accrual_journals','cash_basis_amount_as_of_month':round(cutoff_cash_total,2),'cash_basis_row_count_as_of_month':cutoff_cash_count,'as_of_date':cutoff.isoformat() if cutoff else None,'scope_as_of_month':'property_split_rows_through_reporting_cutoff'}
def monthly_financial_summary(property_name,financials_md,index,source_ledger=None,run_month=None,cutoff_date=None):
    financials_reserve=financials_md_lofty_reserve(financials_md)
    pm_reserve=match_lofty_reserve(property_name,index)
    reserve=dict(pm_reserve or (financials_reserve if not index else {}) or {})
    if pm_reserve:
        # PM/SDK snapshots are fresher for live reserve/rent fields than approved local summaries.
        if pm_reserve.get('curr_maintenance_reserve') is not None:
            reserve.update({
                'curr_maintenance_reserve':pm_reserve.get('curr_maintenance_reserve'),
                'address':pm_reserve.get('address'),
                'source':pm_reserve.get('source'),
                'source_mode':pm_reserve.get('source_mode') or 'pm_or_sdk_snapshot',
            })
        if reserve.get('monthly_rent') is None and pm_reserve.get('monthly_rent') is not None:
            reserve['monthly_rent']=pm_reserve.get('monthly_rent')
            reserve['source']=pm_reserve.get('source')
            reserve['source_mode']=pm_reserve.get('source_mode') or 'pm_or_sdk_snapshot'
    gl=gl_column_e_sum(financials_md,property_name,source_ledger,run_month,cutoff_date)
    rent=financials_md_monthly_rent(financials_md)
    reserve_net=None
    reserve_property=canonical_reserve_property(property_name)
    reserve_ledger=Path(str(gl.get('path') or ''))
    if reserve_property and run_month and reserve_ledger.is_file():
        reserve_net=float(eco_gl_net_of_accruals(read_reserve_rows(reserve_ledger),reserve_property,run_month))
    summary={'property_name':property_name,'as_of_month':run_month,'lofty_curr_maintenance_reserve':reserve.get('curr_maintenance_reserve') if reserve else None,'lofty_curr_maintenance_reserve_source':reserve.get('address') if reserve else None,'lofty_curr_maintenance_reserve_source_file':reserve.get('source') if reserve else None,'lofty_curr_maintenance_reserve_source_mode':reserve.get('source_mode') if reserve else None,'lofty_monthly_rent':(rent.get('monthly_rent') if rent else None) if (rent and rent.get('monthly_rent') is not None) else (reserve.get('monthly_rent') if reserve else None),'lofty_monthly_rent_source':rent.get('source') if rent else (reserve.get('source') if reserve and reserve.get('monthly_rent') is not None else None),'lofty_monthly_rent_source_mode':rent.get('source_mode') if rent else ('pm_or_sdk_snapshot' if reserve and reserve.get('monthly_rent') is not None else None),'eco_gl_column_e_sum':gl.get('amount'),'eco_general_ledger_sum':gl.get('amount'),'eco_operating_cash':gl.get('amount'),'eco_operating_cash_as_of_month':gl.get('amount_as_of_month'),'eco_operating_cash_status':'ok' if gl.get('status')=='ok' else 'missing_gl_source','eco_operating_cash_source_mode':gl.get('source_mode'),'eco_operating_cash_source':gl.get('path'),'eco_operating_cash_as_of_date':gl.get('as_of_date'),'eco_operating_cash_balance_scope':gl.get('scope') or ('all_property_split_rows' if gl.get('status')=='ok' else None),'eco_cash_basis_amount':gl.get('cash_basis_amount'),'eco_cash_basis_row_count':gl.get('cash_basis_row_count'),'eco_cash_basis_scope':gl.get('cash_basis_scope'),'eco_cash_basis_amount_as_of_month':gl.get('cash_basis_amount_as_of_month'),'eco_gl_column_e_net_of_accruals':reserve_net,'eco_gl_column_e_source':gl.get('path'),'eco_gl_column_e_row_count':gl.get('row_count'),'eco_gl_column_e_status':gl.get('status'),'eco_gl_column_e_source_mode':gl.get('source_mode'),'eco_gl_column_e_scope':gl.get('scope') or ('all_property_split_rows' if gl.get('status')=='ok' else None),'eco_gl_column_e_sum_as_of_month':gl.get('amount_as_of_month'),'eco_gl_column_e_row_count_as_of_month':gl.get('row_count_as_of_month'),'eco_gl_column_e_as_of_date':gl.get('as_of_date'),'eco_gl_column_e_scope_as_of_month':gl.get('scope_as_of_month'),'property_cash_summary_adjustments':property_cash_summary_adjustments(property_name)}
    snapshot=ledger_cash_flow_snapshot(summary,run_month)
    summary['retained_capital']=snapshot.get('retained_capital') if snapshot.get('status')=='ok' else None
    return summary
def has_verified_financial_summary(s): return s.get('eco_gl_column_e_status')=='ok'
def render_monthly_financial_summary(s):
    cash_line=f"- ECO Operating Cash: {format_money(s.get('eco_operating_cash'))}"
    cash_line+=" (full ECO General Ledger net position, including accruals)"
    as_of=f" as of {s.get('as_of_month')}" if s.get('as_of_month') else ""
    lines=[f"Financial summary{as_of}:",f"- Lofty-held current maintenance reserve: {format_money(s.get('lofty_curr_maintenance_reserve'))}",cash_line]
    if parse_money(s.get('retained_capital')): lines.append(f"- Retained capital: {format_money(s.get('retained_capital'))}")
    for item in (s.get('property_cash_summary_adjustments') or {}).get('cash_position_lines') or []:
        if isinstance(item,dict): lines.append(f"- {item.get('metric')}: {format_money(item.get('amount'))} ({item.get('source')})")
    return "\n".join(lines)
FINANCIAL_SUMMARY_BLOCK_RE=re.compile(r"(?ims)\n*Financial summary(?:\s+as\s+of\s+\d{4}-\d{2})?:\s*\n\s*-\s*Lofty-held current maintenance reserve:\s*(?:Not available|-?\$[\d,]+\.\d{2})\s*\n\s*-\s*ECO GL Column E sum:\s*(?:Not available|-?\$[\d,]+\.\d{2}(?:\s+\(\d+\s+rows\))?)\s*")
FINANCIALS_MD_SUMMARY_BLOCK_RE=re.compile(r"(?ims)\n*(?:Financial summary from FINANCIALS\.md:|Financial detail:)\s*\n.*?(?=\n\s*(?:Financial summary from FINANCIALS\.md:|Financial detail:)\s*\n|\Z)")
LOFTY_OPERATING_CASH_SENTENCE_RE=re.compile(r"\*\*Lofty Operating Cash of (?:Not available|-?\$[\d,]+\.\d{2})\*\* held as Lofty `curr_maintenance_reserve`",re.I)
ECO_OPERATING_CASH_SENTENCE_RE=re.compile(r"the ECO Systems General Ledger Column E operating cash balance is \*\*(?:Not available|-?\$[\d,]+\.\d{2})\*\* across \*\*\d+\s+rows\*\*",re.I)
def append_monthly_financial_summary(text,s):
    text=FINANCIAL_SUMMARY_BLOCK_RE.sub('',text).rstrip()
    text=LOFTY_OPERATING_CASH_SENTENCE_RE.sub(f"**Lofty Operating Cash of {format_money(s.get('lofty_curr_maintenance_reserve'))}** held as Lofty `curr_maintenance_reserve`",text)
    text=ECO_OPERATING_CASH_SENTENCE_RE.sub(f"The ECO Systems General Ledger Column E operating cash balance is **{format_money(s.get('eco_gl_column_e_sum'))}** across **{int(s.get('eco_gl_column_e_row_count') or 0)} rows**",text)
    text=text.rstrip()+"\n\n"+render_monthly_financial_summary(s)+"\n"; validate_latest_only_update_candidate(text); return text

def generic_owner_update_draft(t):
    l=t.lower(); return sum(x in l for x in ['proactive owner communication','keep update cadence consistent','property operations and file review are in progress','a fuller operational/financial update will follow','what to expect next'])>=2
def clean_update_body(t):
    t=t.strip(); t=PROPERTY_UPDATES_HEADER_RE.sub('',t).strip(); t=DATED_UPDATE_HEADING_RE.sub('',t).strip(); t=re.sub(r"(?m)^-\s+\*{0,2}Property Update\s*\([^)]*\):\s*",'',t).strip()
    t=LIMITED_UPDATE_LANGUAGE_RE.sub("\n\n",t).strip()
    t=re.sub(r"(?im)^Hi everyone[,.]?\s*$",'',t).strip()
    t=re.sub(rf"(?im)^{re.escape(CURRENT_REVIEWED_FINANCIAL_SUMMARY_SENTENCE)}\s*$",'',t).strip()
    return re.sub(r"\n{3,}","\n\n",t).strip()
def month_label(run_month):
    try: return datetime.strptime(str(run_month),"%Y-%m").strftime("%B %Y")
    except (TypeError,ValueError): return str(run_month or 'the reporting month')
def render_owner_update_key_items(s,run_month=None):
    snap=ledger_cash_flow_snapshot(s,run_month)
    if snap.get('status')!='ok': return "Major items:\n- Current property financials are included below."
    label=month_label(run_month)
    noi=parse_money(snap.get('noi')) or 0.0; debt=parse_money(snap.get('debt_service')) or 0.0; capex=parse_money(snap.get('capital_expenditures')) or 0.0; retained=parse_money(snap.get('retained_capital')) or 0.0; nocf=parse_money(snap.get('net_operating_cashflow')) or 0.0
    lines=[f"Major items for {label}:",f"- Revenue was {format_money(snap.get('revenue'))} and operating expenses were {format_money(snap.get('operating_expenses'))}, resulting in NOI of {format_money(noi)}."]
    if debt:
        lines.append(f"- Debt service was {format_money(abs(debt))}; Net Operating Cashflow after debt service was {format_money(nocf)}.")
    elif capex:
        lines.append(f"- Capital expenditures were {format_money(abs(capex))}; Net Operating Cashflow after capital expenditures was {format_money(nocf)}.")
    elif retained:
        lines.append(f"- Retained Capital was {format_money(retained)}; Net Operating Cashflow was {format_money(nocf)}.")
    else:
        lines.append(f"- Net Operating Cashflow was {format_money(nocf)}.")
    if snap.get('cash_flow_annualization_policy')=='scheduled_rent_run_rate_excess_cash_not_annualized':
        lines.append(f"- Rent receipts exceeded scheduled monthly rent by {format_money(snap.get('excess_cash_revenue'))}; that excess is retained unless explicitly approved, not recurring cash flow for Lofty CoC/annualization.")
    elif snap.get('cash_flow_annualization_policy')=='review_required_unattributed_multi_rent_cash_not_annualized':
        lines.append(f"- Rent receipts were split across {snap.get('revenue_row_count')} rows without an approved rent basis; excess cash is retained and not annualized until PM/SDK rent evidence is attached.")
    lines.append(f"- Lofty-held reserve is {format_money(s.get('lofty_curr_maintenance_reserve'))}; ECO Net DAO Funds is {format_money(s.get('eco_operating_cash'))} from the full ECO General Ledger net position, including accruals.")
    for item in (s.get('property_cash_summary_adjustments') or {}).get('cash_position_lines') or []:
        if isinstance(item,dict): lines.append(f"- {item.get('metric')} is {format_money(item.get('amount'))} ({item.get('source')}).")
    return "\n".join(lines)
def ensure_owner_update_key_items(text,s,run_month=None):
    if re.search(r"(?m)^Major items(?: for [^:]+)?:\s*$",text): return text
    key_items=render_owner_update_key_items(s,run_month)
    marker=re.search(r"(?m)^-\s+Property Update\s*\([^)]*\):\s*$",text)
    if not marker: return key_items+"\n\n"+text.lstrip()
    return text[:marker.end()]+"\n"+key_items+"\n\n"+text[marker.end():].lstrip()
def update_entry_from_financial_summary(name,entry_date,s):
    if not has_verified_financial_summary(s): raise ValueError(f"missing owner update draft and verified financial summary for {name}")
    body=render_owner_update_key_items(s,s.get('as_of_month'))
    return f"## {entry_date.isoformat()}\n\n- Property Update ({entry_date:%m/%d/%Y}):\n{body}\n"
def update_entry_from_draft(path,entry_date):
    if path.name=='UPDATES.md': raise ValueError(f"owner update draft cannot be raw UPDATES.md: {path}")
    raw=path.read_text(encoding='utf-8',errors='replace')
    if generic_owner_update_draft(raw): raise ValueError(f"owner update draft is generic placeholder text: {path}")
    body=clean_update_body(raw)
    if not body: raise ValueError(f"empty clean update body from {path}")
    text=f"## {entry_date.isoformat()}\n\n- Property Update ({entry_date:%m/%d/%Y}):\n{body}\n"; validate_latest_only_update_candidate(text); return text
def validate_latest_only_update_candidate(text):
    m=len(PROPERTY_UPDATE_MARKER_RE.findall(text)); h=len(DATED_UPDATE_HEADING_RE.findall(text))
    if m!=1: raise ValueError(f"owner update candidate must contain exactly one Property Update marker; found {m}")
    if h!=1: raise ValueError(f"owner update candidate must contain exactly one dated heading; found {h}")
    if PROPERTY_UPDATES_HEADER_RE.search(text): raise ValueError('owner update candidate must not include full UPDATES.md history header')
    if len(text)>MAX_OWNER_UPDATE_CHARS: raise ValueError(f"owner update candidate too long: {len(text)}>{MAX_OWNER_UPDATE_CHARS}")

def render_financials_cash_position(s,run_month=None):
    ledger_src='ECO Systems General Ledger Column E'
    if s.get('eco_gl_column_e_status')=='ok' and s.get('eco_gl_column_e_row_count') is not None: ledger_src+=f" ({int(s.get('eco_gl_column_e_row_count') or 0)} rows)"
    head='## Monthly Cash Position'+(f" ({run_month})" if run_month else '')
    rows=[f"| Lofty Operating Cash | {format_money(s.get('lofty_curr_maintenance_reserve'))} | Lofty `curr_maintenance_reserve` |",f"| ECO Operating Cash | {format_money(s.get('eco_operating_cash'))} | {ledger_src} |",f"| Physical Baselane Bank Cash | {format_money(s.get('physical_bank_cash'))} | Dated reconciliation evidence as of {s.get('physical_bank_cash_as_of_date') or 'unavailable'} |"]
    rows.extend(cash_position_adjustment_rows(s))
    return "\n".join([head,'',"Revenue and operating expenses are scoped to the reporting month. ECO Operating Cash/ECO Net DAO Funds is the full property General Ledger net position, including accruals. Physical bank cash is separate reconciliation evidence.",'','| Metric | Amount | Source |','|---|---:|---|',*rows])
def append_financials_cash_position(text,s,run_month=None):
    pat=re.compile(r"(?ms)\n*## Monthly Cash Position(?:\s+\([^)]+\))?\s*\n.*?(?=^##\s+|\Z)")
    block=render_financials_cash_position(s,run_month)
    text=pat.sub('\n\n',text).rstrip()
    return (text+'\n\n'+block+'\n').lstrip()
def bucket(row):
    if 'aops-pnl-accrual|retained_capital|' in str(row.get('Notes') or '').lower(): return 'retained_capital'
    transaction_text=norm(' '.join(str(row.get(k) or '') for k in ('Merchant','Description')))
    if 'internal transfer' in transaction_text: return 'inter_account_transfer'
    n=norm(' '.join(str(row.get(k) or '') for k in ('Type','Category','Sub-category')))
    category_n=norm(' '.join(str(row.get(k) or '') for k in ('Category','Sub-category')))
    if 'insurance' in n or 'rental dwelling' in n: return 'insurance'
    if 'revenue' in n or 'rent' in n: return 'rents'
    if 'capex' in category_n or 'capital expenditure' in category_n or 'remodel' in category_n: return 'capex'
    if 'loan payments' in n or 'mortgage payment' in n: return 'debt_service'
    if 'utility' in n: return 'utilities'
    if 'tax' in n: return 'taxes'
    if 'management' in n or 'pm fee' in n: return 'property_mgmt_fee'
    if 'software' in n or 'subscription' in n: return 'software_subscriptions'
    if 'legal' in n or 'professional' in n: return 'legal_professional'
    if 'clean' in n or 'maintenance' in n: return 'cleaning_maintenance'
    if 'repair' in n or 'supply' in n or 'expense' in n: return 'repairs_supplies'
    return None
def source_month_row_count(path,run_month):
    if not run_month: return 0
    with path.open(newline='',encoding='utf-8-sig',errors='replace') as h:
        return sum(1 for r in csv.DictReader(h) if row_date_in_run_month(str(r.get('Date') or r.get('date') or ''),run_month))
def row_date_in_run_month(raw,run_month):
    raw=str(raw or '').strip()
    if not run_month: return True
    if raw.startswith(run_month): return True
    for fmt in ("%B %d, %Y","%b %d, %Y","%m/%d/%Y","%-m/%-d/%Y"):
        try:
            return datetime.strptime(raw,fmt).strftime("%Y-%m")==run_month
        except ValueError:
            continue
    return False
def ledger_cash_flow_snapshot(s,run_month):
    if s.get('eco_gl_column_e_source_mode')=='source_ledger_zero_rows':
        return {
            'status':'ok',
            'ledger_path':s.get('eco_gl_column_e_source'),
            'source_month_row_count':0,
            'revenue':0.0,
            'operating_expenses':0.0,
            'noi':0.0,
            'debt_service':0.0,
            'capital_expenditures':0.0,
            'retained_capital':0.0,
            'net_operating_cashflow':0.0,
            'revenue_bucket_count':0,
            'operating_expense_bucket_count':0,
        }
    path=Path(str(s.get('eco_gl_column_e_source') or ''))
    if not path.is_file(): return {'status':'missing_ledger'}
    b={}; revenue_row_count=0
    with path.open(newline='',encoding='utf-8-sig',errors='replace') as h:
        for row in csv.DictReader(h):
            d=str(row.get('Date') or row.get('date') or '')
            if run_month and d and not row_date_in_run_month(d,run_month): continue
            a=parse_money(row.get('Amount') if 'Amount' in row else row.get('amount')); k=bucket(row)
            if a is not None and k:
                b[k]=b.get(k,0.0)+a
                if k in REV and a > 0: revenue_row_count+=1
    rev=round(sum(b.get(k,0.0) for k in REV),2); op=round(sum(b.get(k,0.0) for k in OPEX),2); noi=round(rev+op,2); debt_service=round(b.get('debt_service',0.0),2); capex=round(b.get('capex',0.0),2); retained=round(b.get('retained_capital',0.0),2); net=round(noi+debt_service+capex+retained,2)
    scheduled_rent=parse_money(s.get('lofty_monthly_rent') or s.get('monthly_rent'))
    recurring_revenue=rev; excess_revenue=0.0; annualization_policy='monthly_net_operating_cashflow'
    if scheduled_rent is not None and scheduled_rent > 0 and rev > round(scheduled_rent*1.25,2):
        recurring_revenue=round(scheduled_rent,2); excess_revenue=round(rev-recurring_revenue,2); annualization_policy='scheduled_rent_run_rate_excess_cash_not_annualized'
    elif scheduled_rent is None and rev > 0 and revenue_row_count > 1:
        recurring_revenue=0.0; excess_revenue=rev; annualization_policy='review_required_unattributed_multi_rent_cash_not_annualized'
    recurring_noi=round(recurring_revenue+op,2); recurring_net=round(recurring_noi+debt_service+capex+retained,2); projected_basis=round(max(recurring_net,0.0)*12,2)
    return {'status':'ok','ledger_path':str(path),'source_month_row_count':source_month_row_count(path,run_month),'revenue':rev,'operating_expenses':op,'noi':noi,'debt_service':debt_service,'capital_expenditures':capex,'retained_capital':retained,'net_operating_cashflow':net,'revenue_bucket_count':sum(1 for k in REV if b.get(k)),'revenue_row_count':revenue_row_count,'operating_expense_bucket_count':sum(1 for k in OPEX if b.get(k)),'scheduled_monthly_rent':scheduled_rent,'recurring_revenue_basis':recurring_revenue,'excess_cash_revenue':excess_revenue,'recurring_noi':recurring_noi,'recurring_net_operating_cashflow':recurring_net,'projected_annual_cash_flow_basis':projected_basis,'cash_flow_annualization_policy':annualization_policy}
def render_source_backed_financial_snapshot(s,run_month):
    snap=ledger_cash_flow_snapshot(s,run_month)
    if snap.get('status')!='ok': raise ValueError(f"cannot build source-backed financial snapshot: {snap.get('status')}")
    metrics=[f"| Revenue | {format_money(snap.get('revenue'))} |",f"| Operating Expenses | {format_money(snap.get('operating_expenses'))} |",f"| NOI | {format_money(snap.get('noi'))} |"]
    if parse_money(snap.get('debt_service')): metrics.append(f"| Debt Service | {format_money(snap.get('debt_service'))} |")
    if parse_money(snap.get('capital_expenditures')): metrics.append(f"| Capital Expenditures | {format_money(snap.get('capital_expenditures'))} |")
    if parse_money(snap.get('retained_capital')): metrics.append(f"| Retained Capital | {format_money(snap.get('retained_capital'))} |")
    metrics.append(f"| Net Operating Cashflow | {format_money(snap.get('net_operating_cashflow'))} |")
    if snap.get('cash_flow_annualization_policy') in {'scheduled_rent_run_rate_excess_cash_not_annualized','review_required_unattributed_multi_rent_cash_not_annualized'}:
        metrics.extend([f"| Scheduled Monthly Rent | {format_money(snap.get('scheduled_monthly_rent'))} |",f"| Excess Cash Revenue | {format_money(snap.get('excess_cash_revenue'))} |",f"| Recurring Revenue Basis | {format_money(snap.get('recurring_revenue_basis'))} |",f"| Recurring NOI | {format_money(snap.get('recurring_noi'))} |",f"| Recurring Net Operating Cashflow | {format_money(snap.get('recurring_net_operating_cashflow'))} |",f"| Projected Annual Cash Flow Basis | {format_money(snap.get('projected_annual_cash_flow_basis'))} |"])
    evidence=[f"| Source month | {run_month} |",f"| ECO GL source | `{snap.get('ledger_path')}` |",f"| Source-month dated rows | {snap.get('source_month_row_count')} |",f"| Revenue bucket count | {snap.get('revenue_bucket_count')} |",f"| Operating expense bucket count | {snap.get('operating_expense_bucket_count')} |",f"| Cash Flow Annualization Policy | {snap.get('cash_flow_annualization_policy')} |",f"| ECO GL Column E rows | {int(s.get('eco_gl_column_e_row_count') or 0)} |",f"| ECO GL Column E sum | {format_money(s.get('eco_gl_column_e_sum'))} |"]
    evidence.extend(source_evidence_adjustment_rows(s))
    return "\n".join(['# Financials','',f'## Cash Flow Snapshot ({run_month})','','| Metric | Amount |','|---|---:|',*metrics,'',render_financials_cash_position(s,run_month),'','## Source Evidence','','| Field | Value |','|---|---|',*evidence])+"\n"
def financials_md_summary_sections(financial_text, run_month=None):
    sections=[]
    for heading in ('Cash Flow Snapshot','Monthly Cash Position','Source Evidence'):
        if run_month:
            pattern=rf"(?ms)^##\s+{re.escape(heading)}\s+\({re.escape(str(run_month))}\)\s*\n.*?(?=^##\s+|\Z)"
        else:
            pattern=rf"(?ms)^##\s+{re.escape(heading)}(?:\s+\([^)]+\))?\s*\n.*?(?=^##\s+|\Z)"
        matches=list(re.finditer(pattern,financial_text or ''))
        if matches: sections.append(matches[-1].group(0).strip())
    return sections
def plain_text_financials_section(section):
    lines=[]
    for raw in str(section or '').splitlines():
        line=raw.strip()
        if not line:
            if lines and lines[-1]: lines.append('')
            continue
        if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$",line): continue
        if line.startswith('|') and line.endswith('|'):
            cells=[c.strip().replace('`','') for c in line.strip('|').split('|')]
            lowered=[c.lower() for c in cells]
            if lowered in (['metric','amount','source'],['metric','amount'],['field','value']): continue
            if len(cells)>=3: lines.append(f"{cells[0]}: {cells[1]} ({cells[2]})")
            elif len(cells)==2: lines.append(f"{cells[0]}: {cells[1]}")
            continue
        lines.append(line.replace('`',''))
    return "\n".join(lines).strip()
def append_financials_md_summary(text,financial_text,run_month=None):
    sections=financials_md_summary_sections(financial_text,run_month)
    if not sections: raise ValueError('FINANCIALS.md summary sections missing from financial candidate')
    text=FINANCIALS_MD_SUMMARY_BLOCK_RE.sub('',text).rstrip()
    text=FINANCIAL_SUMMARY_BLOCK_RE.sub('',text).rstrip()
    text=LOFTY_OPERATING_CASH_SENTENCE_RE.sub('',text)
    text=ECO_OPERATING_CASH_SENTENCE_RE.sub('',text).rstrip()
    text=text.rstrip()+"\n\n"+FINANCIAL_DETAIL_HEADING+"\n\n"+"\n\n".join(plain_text_financials_section(s) for s in sections)+"\n"
    validate_latest_only_update_candidate(text)
    return text

def financials_md_summary_text(financial_text,run_month=None):
    sections=financials_md_summary_sections(financial_text,run_month)
    if not sections: return None
    return FINANCIAL_DETAIL_HEADING+"\n\n"+"\n\n".join(plain_text_financials_section(s) for s in sections)+"\n"

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
def sha256_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
def populated_candidate_packet(path:Path):
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(payload,dict): return None
    records=payload.get('records') if isinstance(payload.get('records'),list) else []
    if records and int(payload.get('property_count') or len(records) or 0)>0:
        return payload
    return None
def financial_candidate_gate_issues(text):
    l=text.lower(); issues=[]
    if 'generated deterministically' in l or 'review before investor email/publish' in l: issues.append('generated_ledger_financial_source_requires_source_backed_snapshot')
    if '{property' in text or 'tbd' in l or 'raw pool diagnostics' in l: issues.append('financial_candidate_template_or_diagnostic_marker')
    return issues
def cash_flow_snapshot_months(text):
    return re.findall(r"(?im)^##\s+Cash Flow Snapshot\s+\((\d{4}-\d{2})\)\s*$", text or '')
def likely_unavailable_placeholder(path):
    try:
        st=path.stat()
    except OSError:
        return False
    return st.st_size>0 and getattr(st,'st_blocks',1)==0
def copy_financial_candidate(path,s=None,run_month=None):
    if not path.is_file() and s and has_verified_financial_summary(s):
        return render_source_backed_financial_snapshot(s,run_month)
    if s and has_verified_financial_summary(s) and likely_unavailable_placeholder(path):
        return render_source_backed_financial_snapshot(s,run_month)
    text=path.read_text(encoding='utf-8',errors='replace').strip()
    if not text: raise ValueError(f"empty financials source: {path}")
    text='\n'.join('- **Liquidity Score:** Available in Lofty market data; raw pool diagnostics omitted from owner review candidate.' if line.strip().startswith('- **Liquidity Score:** {') else line.replace('TBD','Not available in local source') for line in text.splitlines()).strip()
    generated='## Cash Flow Snapshot' in text and '## Monthly Cash Position' in text and '## Source Evidence' in text
    snapshot_months=cash_flow_snapshot_months(text)
    stale_snapshot=bool(run_month and snapshot_months and run_month not in snapshot_months)
    if s and has_verified_financial_summary(s) and (financial_candidate_gate_issues(text) or generated or stale_snapshot): return render_source_backed_financial_snapshot(s,run_month)
    return append_financials_cash_position(text,s,run_month) if s else text+'\n'

def financial_candidate_snapshot_evidence(s,run_month=None):
    if not s or not has_verified_financial_summary(s):
        return {'status':'missing_verified_financial_summary'}
    snap=ledger_cash_flow_snapshot(s,run_month)
    ledger_path=snap.get('ledger_path')
    return {
        'status':snap.get('status'),
        'ledger_path':ledger_path,
        # A monthly candidate is only publishable against the exact ledger it summarized.
        'ledger_sha256':sha256_file(ledger_path) if ledger_path else None,
        'source_month_row_count':snap.get('source_month_row_count'),
        'revenue':snap.get('revenue'),
        'operating_expenses':snap.get('operating_expenses'),
        'noi':snap.get('noi'),
        'debt_service':snap.get('debt_service'),
        'capital_expenditures':snap.get('capital_expenditures'),
        'retained_capital':snap.get('retained_capital'),
        'net_operating_cashflow':snap.get('net_operating_cashflow'),
        'revenue_bucket_count':snap.get('revenue_bucket_count'),
        'operating_expense_bucket_count':snap.get('operating_expense_bucket_count'),
    }

def update_approval_target(r,run_month):
    return str(r.get('update_review_target')) if str(r.get('update_review_target') or '').strip() else (str(Path(str(r.get('updates_md'))).parent/f"{run_month}-owner-update-approved.md") if str(r.get('updates_md') or '').strip() else None)
def financial_approval_target(r,run_month):
    return None
def section_excluded_or_skipped(s): return str(s or '').startswith(('skipped_','excluded_'))
def live_publish_exclusion(property_path):
    guards,_,_=monthly_exclusion_guards(None)
    return match_exclusion_guard(property_path,guards)
def remove_stale_candidate(p):
    try:
        if p.is_file(): p.unlink()
    except OSError: pass
def suspicious_markers(text):
    out=[]; l=text.lower()
    for name,pat in {'draft_marker':r'\bdraft\b','review_marker':r'review before sending','unchecked_checklist':r'(?m)^\s*[-*]\s+\[\s\]','full_updates_header':r'(?m)^# Property Updates\s*$'}.items():
        if re.search(pat,text,re.I): out.append(name)
    if 'send-property-updates' in l: out.append('native_lofty_email_action')
    return out

def build_packet(manifest,output_dir:Path,entry_date:date,lofty_profile_json=None,source_ledger=None,lofty_properties_dir=None,lofty_all_properties_json=None,dao_cash_report=None,reporting_cutoff_date=None):
    output_dir=output_dir.resolve(); records=[]; issue_count=update_count=fin_count=marker_count=gate_count=financials_md_summary_count=synthetic_summary_count=0
    index=load_lofty_reserve_index(lofty_profile_json,lofty_properties_dir,lofty_all_properties_json)
    dao_cash_authority=load_dao_cash_report(Path(dao_cash_report or DEFAULT_DAO_CASH_REPORT))
    manifest_records=manifest.get('records') or []
    skipped_excluded_count=0
    for r in manifest_records:
        if not isinstance(r,dict): continue
        if section_excluded_or_skipped(r.get('update_status')) and section_excluded_or_skipped(r.get('financial_status')): skipped_excluded_count+=1; continue
        raw=str(r.get('property_name') or 'property'); managed_name=str(r.get('managed_name') or '').strip(); prop_path,meta=resolve_property_path(Path(str(r.get('property_path') or ''))); name=raw if meta.get('property_path_resolution')=='flat_public_to_nested_public' else (display_name_for_property_path(prop_path,meta) if prop_path.exists() else raw); summary_name=managed_name or name
        outdir=output_dir/slugify(name); outdir.mkdir(parents=True,exist_ok=True); um=outdir/f"{manifest.get('run_month')}-owner-update-review-candidate.md"; fm=outdir/f"{manifest.get('run_month')}-FINANCIALS-review-candidate.md"; remove_stale_candidate(um); remove_stale_candidate(fm)
        exclusion=live_publish_exclusion(prop_path)
        rec={'property_name':name,'managed_name':managed_name or None,'input_property_name':raw,'property_path':str(prop_path),'input_property_path':r.get('property_path'),**meta,'updates_md':r.get('updates_md'),'financials_md':r.get('financials_md'),'update_candidate':str(um),'financial_candidate':str(fm),'update_approval_target':update_approval_target(r,str(manifest.get('run_month') or '')),'financial_approval_target':financial_approval_target(r,str(manifest.get('run_month') or '')),'live_publish_excluded':bool(exclusion),'live_publish_exclusion_source':exclusion.get('source') if exclusion else None,'live_publish_exclusion_reason':exclusion.get('exclude_reason') if exclusion else None,'monthly_financial_summary':{},'financial_candidate_snapshot':{},'update_source_mode':None,'issues':[],'markers':[],'financial_candidate_gate_issues':[]}
        fin_path=resolve_record_document_path(r.get('financials_md'),prop_path,'00 - README & Property Snapshot')
        rec['financials_md']=str(fin_path)
        rec['financial_approval_target']=None
        rec['monthly_financial_summary']=apply_dao_cash_to_summary(monthly_financial_summary(summary_name,fin_path,index,source_ledger,str(manifest.get('run_month') or '') or None,reporting_cutoff_date),summary_name,dao_cash_authority)
        summary=rec['monthly_financial_summary'] if isinstance(rec.get('monthly_financial_summary'),dict) else {}
        if summary.get('eco_gl_column_e_source_mode')=='source_ledger_zero_rows' and summary.get('eco_gl_column_e_row_count')==0:
            rec['zero_row_source_ledger_reviewed']=True
            rec['zero_row_source_ledger_decision']='include_active_no_activity'
            rec['zero_row_source_ledger_reason']='active monthly manifest record has no property-split GL and no matching source-ledger rows; do not infer ECO Operating Cash from the empty ledger'
        rec['financial_candidate_snapshot']=financial_candidate_snapshot_evidence(rec['monthly_financial_summary'],str(manifest.get('run_month') or '') or None)
        if fin_path.is_file() or rec['monthly_financial_summary'].get('eco_gl_column_e_status')=='ok':
            if rec['monthly_financial_summary'].get('lofty_curr_maintenance_reserve') is None:
                if rec['live_publish_excluded']:
                    rec['local_reporting_notes']=['monthly financial summary missing Lofty curr_maintenance_reserve; live publish is excluded by policy']
                else: issue_count+=1; rec['issues'].append('monthly financial summary missing Lofty curr_maintenance_reserve')
            if rec['monthly_financial_summary'].get('eco_gl_column_e_status')!='ok': issue_count+=1; rec['issues'].append('monthly financial summary missing ECO GL Column E source')
            if rec['monthly_financial_summary'].get('physical_bank_cash_status')!='ok':
                rec['bank_reconciliation_notes']=['No mapped live Baselane DAO bank account was found. This does not invalidate the canonical General Ledger or ECO Net DAO Funds; physical cash transfer execution remains subject to separate account reconciliation.']
        fin_text=None
        try:
            fin_text=copy_financial_candidate(fin_path,rec['monthly_financial_summary'],str(manifest.get('run_month') or ''))
            fm.write_text(fin_text,encoding='utf-8'); fin_count+=1; rec['markers'].extend('financial.'+m for m in suspicious_markers(fin_text)); gi=financial_candidate_gate_issues(fin_text)
            if gi: gate_count+=len(gi); rec['financial_candidate_gate_issues']=gi; rec['issues'].extend(gi); issue_count+=len(gi)
        except Exception as e: issue_count+=1; rec['issues'].append(f'financial candidate failed: {e}')
        try:
            draft=resolve_record_document_path(r.get('draft_path'),prop_path,'00 - README & Property Snapshot')
            if draft.is_file():
                raw_text=draft.read_text(encoding='utf-8',errors='replace')
                if generic_owner_update_draft(raw_text) and has_verified_financial_summary(rec['monthly_financial_summary']): text=update_entry_from_financial_summary(name,entry_date,rec['monthly_financial_summary']); rec['update_source_mode']='financial_summary_fallback_generic_draft'
                else: text=update_entry_from_draft(draft,entry_date); rec['update_source_mode']='draft'
            else: text=update_entry_from_financial_summary(name,entry_date,rec['monthly_financial_summary']); rec['update_source_mode']='financial_summary_fallback_missing_draft'
            if rec['monthly_financial_summary']:
                text=ensure_owner_update_key_items(text,rec['monthly_financial_summary'],str(manifest.get('run_month') or '') or None)
                if fin_text:
                    summary_text=financials_md_summary_text(fin_text,str(manifest.get('run_month') or '') or None)
                    text=append_financials_md_summary(text,fin_text,str(manifest.get('run_month') or '') or None)
                    rec['financial_summary_source_mode']='financials_md'; rec['financials_md_summary_sha256']=sha256_text(summary_text or ''); rec['financials_md_summary_char_count']=len(summary_text or ''); financials_md_summary_count+=1
                else:
                    text=append_monthly_financial_summary(text,rec['monthly_financial_summary']); rec['financial_summary_source_mode']='synthetic'; synthetic_summary_count+=1
            um.write_text(text,encoding='utf-8'); update_count+=1; rec['markers'].extend('update.'+m for m in suspicious_markers(text))
        except Exception as e: issue_count+=1; rec['issues'].append(f'update candidate failed: {e}')
        marker_count+=len(rec['markers']); records.append(rec)
    empty_reason=None
    if not records:
        if not manifest_records: empty_reason='review_manifest_has_no_records'
        elif skipped_excluded_count==len([r for r in manifest_records if isinstance(r,dict)]): empty_reason='all_manifest_records_excluded_or_skipped'
        else: empty_reason='no_candidate_records_generated'
    manifest_source_issues=manifest.get('source_issues') if isinstance(manifest.get('source_issues'),list) else []
    missing_reserve_records=[{'property_name':r.get('property_name'),'managed_name':r.get('managed_name'),'input_property_name':r.get('input_property_name'),'financials_md':r.get('financials_md')} for r in records if not r.get('live_publish_excluded') and isinstance(r.get('monthly_financial_summary'),dict) and r.get('monthly_financial_summary',{}).get('lofty_curr_maintenance_reserve') is None]
    missing_reserve_artifacts=write_missing_reserve_artifacts(output_dir,missing_reserve_records,manifest.get('run_month'))
    return {'generated_at':iso_z(),'status':'review' if issue_count or marker_count or empty_reason else 'ok','run_month':manifest.get('run_month'),'entry_date':entry_date.isoformat(),'reporting_cutoff_date':reporting_cutoff_date.isoformat() if reporting_cutoff_date else None,'lofty_profile_json':str(lofty_profile_json) if lofty_profile_json else None,'lofty_properties_dir':str(lofty_properties_dir) if lofty_properties_dir else None,'lofty_all_properties_json':str(lofty_all_properties_json) if lofty_all_properties_json else None,'source_ledger':str(source_ledger) if source_ledger else None,'lofty_reserve_index_count':len(index),'lofty_reserve_index_source_counts':reserve_index_source_counts(index),'missing_lofty_reserve_count':len(missing_reserve_records),'missing_lofty_reserve_records':missing_reserve_records[:100],'missing_lofty_reserve_csv':missing_reserve_artifacts['missing_lofty_reserve_csv'],'missing_lofty_reserve_markdown':missing_reserve_artifacts['missing_lofty_reserve_markdown'],'output_dir':str(output_dir),'manifest_record_count':len(manifest_records),'manifest_status':manifest.get('status'),'manifest_source_issue_count':int(manifest.get('source_issue_count') or len(manifest_source_issues)),'review_manifest_source_issues':manifest_source_issues,'skipped_excluded_record_count':skipped_excluded_count,'empty_candidate_packet_reason':empty_reason,'next_action':'Regenerate monthly review manifest from non-empty guarded apply output before Discord/email/transfer reconciliation.' if empty_reason=='review_manifest_has_no_records' else None,'property_count':len(records),'update_candidate_count':update_count,'financial_candidate_count':fin_count,'issue_count':issue_count,'marker_count':marker_count,'financial_candidate_gate_issue_count':gate_count,'financials_md_summary_update_count':financials_md_summary_count,'synthetic_summary_update_count':synthetic_summary_count,'records':records}
def render_markdown(report):
    lines=['# Lofty Monthly Review Candidate Packet','',f"- Run month: `{report['run_month']}`",f"- Entry date: `{report['entry_date']}`",f"- Status: `{report['status']}`",f"- Update candidates: `{report['update_candidate_count']}`",f"- Financial candidates: `{report['financial_candidate_count']}`",f"- Issues: `{report['issue_count']}`",f"- Markers: `{report['marker_count']}`",f"- Financial candidate gate issues: `{report.get('financial_candidate_gate_issue_count',0)}`",'','These are review candidates only. They are not approved artifacts and guarded apply will not consume them until a reviewer copies approved text to the manifest approval targets.','','## Candidate Files','']
    for r in report['records']:
        lines += [f"### {r['property_name']}",f"- Update candidate: `{r['update_candidate']}`",f"- Update approval target: `{r.get('update_approval_target')}`",f"- Financial candidate: `{r['financial_candidate']}`",f"- Financial approval target: `{r.get('financial_approval_target')}`"]
        lines += [f"- Issue: {i}" for i in r['issues']] + [f"- Marker: `{m}`" for m in r['markers']] + ['']
    return '\n'.join(lines)
def main(argv=None):
    ap=argparse.ArgumentParser(description='Generate clean review candidate files from monthly Lofty drafts without marking them approved.'); ap.add_argument('--manifest',required=True,type=Path); ap.add_argument('--output-dir',required=True,type=Path); ap.add_argument('--entry-date',required=True,type=parse_iso_date); ap.add_argument('--reporting-cutoff-date',type=parse_iso_date,default=None); ap.add_argument('--lofty-profile-json',type=Path,default=None,help='Optional PM/SDK-derived profile snapshot; do not pass LoftyAssist snapshots.'); ap.add_argument('--lofty-properties-dir',type=Path,default=None,help='Optional PM/SDK-derived property snapshots; do not pass LoftyAssist snapshots.'); ap.add_argument('--lofty-all-properties-json',type=Path,default=None,help='Optional PM/SDK-derived all-properties snapshot; do not pass LoftyAssist snapshots.'); ap.add_argument('--source-ledger',type=Path,default=default_source_ledger()); ap.add_argument('--dao-cash-report',type=Path,default=DEFAULT_DAO_CASH_REPORT); ap.add_argument('--report',required=True,type=Path); ap.add_argument('--markdown',required=True,type=Path); a=ap.parse_args(argv)
    profile=a.lofty_profile_json if a.lofty_profile_json and a.lofty_profile_json.exists() else None
    properties_dir=a.lofty_properties_dir if a.lofty_properties_dir and a.lofty_properties_dir.exists() else None
    all_properties=a.lofty_all_properties_json if a.lofty_all_properties_json and a.lofty_all_properties_json.exists() else None
    report=build_packet(json.loads(a.manifest.read_text(encoding='utf-8')),a.output_dir,a.entry_date,profile,a.source_ledger if a.source_ledger.exists() else None,properties_dir,all_properties,a.dao_cash_report,a.reporting_cutoff_date)
    existing=populated_candidate_packet(a.report)
    if report.get('empty_candidate_packet_reason') and existing and os.environ.get('BASELANE_ALLOW_EMPTY_REVIEW_CANDIDATE_PACKET')!='1':
        sidecar=a.report.with_name(a.report.stem+'.empty-manifest-review.json')
        existing_records=existing.get('records') if isinstance(existing.get('records'),list) else []
        review={**report,'status':'review','preserved_existing_report':str(a.report),'preserved_existing_property_count':existing.get('property_count'),'preserved_existing_generated_at':existing.get('generated_at'),'preserved_existing_record_names':[str(r.get('property_name') or '') for r in existing_records[:50] if isinstance(r,dict)],'poison_guard':'empty candidate packet refused to reuse populated canonical packet for sends'}
        sidecar.parent.mkdir(parents=True,exist_ok=True)
        sidecar.write_text(json.dumps(review,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        a.report.parent.mkdir(parents=True,exist_ok=True); a.markdown.parent.mkdir(parents=True,exist_ok=True); a.report.write_text(json.dumps(review,indent=2,sort_keys=True)+'\n',encoding='utf-8'); a.markdown.write_text(render_markdown(review)+'\n',encoding='utf-8')
        print(f"status=review issues={report['issue_count']} properties=0 preserved_existing={existing.get('property_count')} sidecar={sidecar}")
        return 2
    a.report.parent.mkdir(parents=True,exist_ok=True); a.markdown.parent.mkdir(parents=True,exist_ok=True); a.report.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n',encoding='utf-8'); a.markdown.write_text(render_markdown(report)+'\n',encoding='utf-8'); print(f"status={report['status']} issues={report['issue_count']} properties={report['property_count']} updates={report['update_candidate_count']} financials={report['financial_candidate_count']}"); return 0 if report['status'] in {'ok','review'} else 1
if __name__=='__main__': raise SystemExit(main())

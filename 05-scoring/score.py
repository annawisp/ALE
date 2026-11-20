"""Offline scorer. Python 3.10+ standard library only; never executes workbook macros."""
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
import math
import operator
import argparse, ast, csv, hashlib, json, re, zipfile, xml.etree.ElementTree as ET

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def numeric(x):
 if isinstance(x,bool) or x is None:return None
 try:return Decimal(str(x)) if re.fullmatch(r'-?\d+(?:\.\d+)?',str(x)) else None
 except InvalidOperation:return None
def equal(a,b):
 if isinstance(a,dict) and isinstance(b,dict):return set(a)==set(b) and all(equal(a[k],b[k]) for k in a)
 if isinstance(a,list) and isinstance(b,list):
  if len(a)!=len(b):return False
  used=set()
  for x in a:
   match=next((i for i,y in enumerate(b) if i not in used and equal(x,y)),None)
   if match is None:return False
   used.add(match)
  return True
 n,m=numeric(a),numeric(b)
 if n is not None and m is not None:return abs(n-m)<=Decimal('0.0000005')
 return a==b

def xlsx(p):
 sheets={}
 with zipfile.ZipFile(p) as z:
  strings=[]
  if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si',NS)]
  rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
  for sh in ET.fromstring(z.read('xl/workbook.xml')).findall('s:sheets/s:sheet',NS):
   target=rels[sh.attrib['{'+NS['r']+'}id']];path=target.lstrip('/') if target.startswith('/') else 'xl/'+target
   root=ET.fromstring(z.read(path));cells={}
   for c in root.findall('.//s:sheetData/s:row/s:c',NS):
    f=c.find('s:f',NS);v=c.find('s:v',NS);typ=c.attrib.get('t');val=None
    if typ=='inlineStr':val=''.join(c.find('s:is',NS).itertext())
    elif v is not None:
     val=v.text
     if typ=='s':val=strings[int(val)]
     elif typ=='b':val=val=='1'
     elif typ not in ('str','e'):
      try:val=float(val)
      except (ValueError,TypeError):pass
    cells[c.attrib['r']]={'value':val,'formula':f.text if f is not None else None,'type':typ}
   cols=root.find('s:cols',NS)
   sheets[sh.attrib['name']]={'cells':cells,'cols':ET.tostring(cols,encoding='unicode') if cols is not None else '', 'row_heights':{r.attrib['r']:r.attrib.get('ht') for r in root.findall('s:sheetData/s:row',NS)}}
 return sheets

def colnum(x):
 n=0
 for c in x:n=n*26+ord(c)-64
 return n
def colstr(n):
 s=''
 while n:n,r=divmod(n-1,26);s=chr(65+r)+s
 return s
class UnsupportedFormula(Exception):
    """Formula has semantics outside this scorer's documented subset."""


class FormulaError(ValueError):
    """An in-scope spreadsheet operation produced an error."""


def is_number(value):
    # Excel booleans in a referenced cell do not count as numbers for COUNT.
    return type(value) in (int, float)


# Tokens originate only in workbook formula text. REF is an internal AST name;
# a workbook cannot spell it as a function. Python never executes the AST.
_FORMULA_TOKEN = re.compile(
    r'(?P<string>"(?:[^"]|"")*")|'
    r"(?P<ref>(?:(?:'(?:[^']|'')+'|[A-Z_\u4e00-\u9fff][A-Z0-9_\u4e00-\u9fff]*)!)?"
    r"\$?[A-Z]{1,3}\$?[1-9]\d*(?::\$?[A-Z]{1,3}\$?[1-9]\d*)?)(?![\w.(])|"
    r'(?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)|'
    r'(?P<func>[A-Z][A-Z0-9_.]*)|(?P<op><>|<=|>=|[=<>+*/^(),-])|(?P<space>\s+)',
    re.IGNORECASE)
_ARITY = {'IF': (3, 3), 'ROUND': (2, 2), 'ROWS': (1, 1), 'COUNT': (1, 255), 'SUM': (1, 255)}
_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.Pow: operator.pow}
_COMPARISONS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
                ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le}


@lru_cache(maxsize=256)
def parse_formula(formula):
    if len(formula) > 2048:
        raise UnsupportedFormula('公式超过长度上限')
    source = formula.removeprefix('=')
    pos = 0
    tokens = []
    prior_kind = None
    for match in _FORMULA_TOKEN.finditer(source):
        if match.start() != pos:
            raise UnsupportedFormula('含未支持的语法')
        pos = match.end()
        kind, text = match.lastgroup, match.group()
        if kind == 'space':
            continue
        if kind in {'num', 'ref', 'func', 'string'} and prior_kind in {'num', 'ref', 'string', ')'}:
            raise UnsupportedFormula('相邻操作数或区域交集')
        if kind == 'ref':
            sn, separator, addr = text.rpartition('!')
            sn = sn.removeprefix("'").removesuffix("'").replace("''", "'")
            addr = addr.replace('$', '').upper()
            text = 'REF(' + repr(sn if separator else '') + ',' + repr(addr) + ')'
        elif kind == 'string':
            text = repr(text[1:-1].replace('""', '"'))
        elif kind == 'func':
            text = text.upper()
            if text not in _ARITY:
                raise UnsupportedFormula(text)
        elif kind == 'op':
            text = {'=': '==', '<>': '!=', '^': '**'}.get(text, text)
        # Spaces separate tokens so Excel **, // and ==, for instance, stay invalid.
        tokens.append(text)
        prior_kind = match.group() if kind == 'op' else kind
    if pos != len(source):
        raise UnsupportedFormula('含未支持的语法')
    try:
        node = ast.parse(' '.join(tokens), mode='eval').body
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise UnsupportedFormula('无法解析公式') from exc
    if sum(1 for _ in ast.walk(node)) > 256:
        raise UnsupportedFormula('公式超过复杂度上限')

    def visit(n):
        if isinstance(n, ast.Constant) and (is_number(n.value) or isinstance(n.value, str)):
            return
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            if isinstance(n.operand, ast.BinOp) and isinstance(n.operand.op, ast.Pow):
                raise UnsupportedFormula('含前置符号的幂表达式')
            visit(n.operand)
        elif isinstance(n, ast.BinOp) and type(n.op) in _OPERATORS:
            # Excel and Python associate stacked powers/unary negative powers differently.
            if isinstance(n.op, ast.Pow) and any(isinstance(x, (ast.UnaryOp, ast.BinOp))
                                                 for x in (n.left, n.right)):
                raise UnsupportedFormula('复合幂表达式')
            if isinstance(n.op, ast.Pow) and not isinstance(n.right, ast.Constant):
                raise UnsupportedFormula('动态指数')
            visit(n.left)
            visit(n.right)
        elif isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _COMPARISONS:
            visit(n.left)
            visit(n.comparators[0])
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
            name = n.func.id
            if name == 'REF':
                return
            if name not in _ARITY or not _ARITY[name][0] <= len(n.args) <= _ARITY[name][1]:
                raise UnsupportedFormula(f'{name} 的参数')
            if name in {'COUNT', 'ROWS'} and any(not is_ref(arg) for arg in n.args):
                raise UnsupportedFormula(f'{name} 仅支持单元格或矩形区域引用')
            for arg in n.args:
                visit(arg)
        else:
            raise UnsupportedFormula('表达式超出支持范围')
    visit(node)
    return node


def is_ref(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'REF'


@lru_cache(maxsize=256)
def rectangle(addr):
    points = addr.split(':')
    coords = []
    for point in (points[0], points[-1]):
        match = re.fullmatch(r'([A-Z]{1,3})([1-9]\d{0,6})', point)
        if not match:
            raise UnsupportedFormula('无效单元格地址')
        col, row = colnum(match[1]), int(match[2])
        if col > 16384 or row > 1048576:
            raise UnsupportedFormula('地址超出工作表边界')
        coords.append((col, row))
    (left, top), (right, bottom) = coords
    if top > bottom or left > right:
        raise UnsupportedFormula('反向区域')
    return left, top, right, bottom


def number(value):
    if not is_number(value):
        raise UnsupportedFormula('运算需要数值，隐式类型转换待核验')
    if not math.isfinite(value):
        raise FormulaError('非有限数值')
    return value


def calc(book, sheet, cell, overrides=None, stack=None):
    overrides = overrides or {}
    active = set(stack or ())
    memo = {}
    budget = 20000

    def read(sn, addr):
        nonlocal budget
        key = sn + '!' + addr
        if key in overrides:
            return overrides[key]
        if key in memo:
            return memo[key]
        if key in active:
            raise FormulaError('循环引用 ' + key)
        budget -= 1
        if budget < 0 or len(active) >= 48:
            raise UnsupportedFormula('引用超过计算上限')
        data = book[sn]['cells'].get(addr, {})
        if data.get('formula'):
            active.add(key)
            try:
                value = evaluate(parse_formula(data['formula']), sn)
                if isinstance(value, list):
                    raise UnsupportedFormula('公式返回数组')
            finally:
                active.remove(key)
        elif data.get('type') == 'e':
            raise UnsupportedFormula('输入含 Excel 错误值')
        else:
            value = data.get('value')
        memo[key] = value
        return value

    def location(n, current):
        sn, addr = (arg.value for arg in n.args)
        sn = sn or current
        if sn not in book:
            matches = [name for name in book if name.casefold() == sn.casefold()]
            if len(matches) != 1:
                raise FormulaError('引用工作表不存在 ' + sn)
            sn = matches[0]
        return sn, addr, rectangle(addr)

    def evaluate(n, sn):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.UnaryOp):
            a = number(evaluate(n.operand, sn))
            return -a if isinstance(n.op, ast.USub) else a
        if isinstance(n, ast.BinOp):
            a, b = number(evaluate(n.left, sn)), number(evaluate(n.right, sn))
            if isinstance(n.op, ast.Pow) and abs(b) > 16:
                raise UnsupportedFormula('指数超过计算上限')
            a, b = float(a), float(b)
            return number(_OPERATORS[type(n.op)](a, b))
        if isinstance(n, ast.Compare):
            a, b = evaluate(n.left, sn), evaluate(n.comparators[0], sn)
            if isinstance(a, list) or isinstance(b, list):
                raise UnsupportedFormula('数组比较')
            # An empty referenced cell can equal zero or ""; numeric zero is distinct from "".
            if a is None:
                a = '' if isinstance(b, str) else 0
            if b is None:
                b = '' if isinstance(a, str) else 0
            if type(a) is str and type(b) is str:
                a, b = a.casefold(), b.casefold()
            elif not (is_number(a) and is_number(b)) and type(a) is not type(b):
                if isinstance(n.ops[0], (ast.Eq, ast.NotEq)):
                    return isinstance(n.ops[0], ast.NotEq)
                raise UnsupportedFormula('跨类型排序比较')
            return _COMPARISONS[type(n.ops[0])](a, b)
        name = n.func.id
        if name == 'REF':
            target, addr, (left, top, right, bottom) = location(n, sn)
            if ':' not in addr:
                return read(target, addr)
            if (right-left+1)*(bottom-top+1) > 4096:
                raise UnsupportedFormula('取值区域超过上限')
            return [read(target, colstr(c)+str(r)) for r in range(top, bottom+1)
                    for c in range(left, right+1)]
        if name == 'IF':
            condition = evaluate(n.args[0], sn)
            if type(condition) is not bool and not is_number(condition):
                raise UnsupportedFormula('IF 条件超出支持范围')
            return evaluate(n.args[1] if condition else n.args[2], sn)
        if name == 'ROWS':
            _, _, (_, top, _, bottom) = location(n.args[0], sn)
            return bottom - top + 1
        if name in {'COUNT', 'SUM'}:
            numbers = []
            for arg in n.args:
                value = evaluate(arg, sn)
                if not is_ref(arg) and not is_number(value):
                    raise UnsupportedFormula('SUM 的非数值直接参数')
                values = value if isinstance(value, list) else [value]
                numbers.extend(number(v) for v in values if is_number(v))
            return len(numbers) if name == 'COUNT' else sum(numbers)
        if name == 'ROUND':
            a, digits = (number(evaluate(arg, sn)) for arg in n.args)
            if int(digits) != digits or abs(digits) > 15:
                raise UnsupportedFormula('ROUND 的精度')
            try:
                return float(Decimal(str(a)).quantize(Decimal(1).scaleb(-int(digits)), rounding=ROUND_HALF_UP))
            except InvalidOperation as exc:
                raise UnsupportedFormula('ROUND 的数值范围') from exc
        raise UnsupportedFormula(name)

    return read(sheet, cell)

def close_scenarios(source):
    """Exercise the shared monthly-close layout, including each row after inputs are complete."""
    filled = {'明细!C7': 456.75}
    cases = [
        ('原始输入', {}),
        ('修改行政费用', {'明细!C2': 1327.25}),
        ('末笔为数值零', {'明细!C7': 0}),
        ('原始明细且预算零', {'月结!B5': 0}),
        ('末笔为非零', filled),
        ('末笔为非零且预算零', {**filled, '月结!B5': 0}),
    ]
    # A missing total masks changes to other rows. With all amounts populated,
    # each individual perturbation must flow to the right subtotal, total and ratio.
    detail = source['明细']['cells']
    complete = dict(filled)
    for row in range(2, 8):
        cell = f'C{row}'
        if detail.get(cell, {}).get('value') is None:
            complete.setdefault('明细!' + cell, 150.25 * row)
    for row in range(2, 8):
        cell = f'C{row}'
        key = '明细!' + cell
        baseline = complete.get(key, detail.get(cell, {}).get('value'))
        if is_number(baseline):
            cases.append((f'明细齐全且单独修改{cell}', {**complete, key: baseline + 97.25 * row}))
    budget = source['月结']['cells'].get('B5', {}).get('value')
    positive_budget = abs(budget) + 1231.75 if is_number(budget) else 1231.75
    cases.append(('明细齐全且改变非零预算', {**complete, '月结!B5': positive_budget}))
    return cases


def workbook_check(g,task,result,gold_dir):
 if not g.get('workbook'):return True,[]
 out=result/'corrected.xlsx'
 if not out.exists():return False,['缺corrected.xlsx']
 try:
  actual=xlsx(out);ref=xlsx(gold_dir/'corrected.xlsx');src=xlsx(next((task/'原始材料').glob('*.xlsx')))
 except Exception as e:return False,['工作簿无法读取：'+str(e)]
 errors=[];allowed=set(g['workbook']['editable_cells'])
 if set(actual)!=set(src):errors.append('工作表名称或数量改变')
 for sn,sh in src.items():
  if sn not in actual:continue
  if sh['cols']!=actual[sn]['cols']:errors.append(sn+'列宽变化')
  if sh['row_heights']!=actual[sn]['row_heights']:errors.append(sn+'行高变化')
  for cell,before in sh['cells'].items():
   if sn+'!'+cell in allowed:continue
   after=actual[sn]['cells'].get(cell,{})
   if before['formula']:
    if before['formula']!=after.get('formula'):errors.append(sn+'!'+cell+'原公式改变')
   elif not equal(before['value'],after.get('value')):errors.append(sn+'!'+cell+'非授权值改变')
 if g['workbook']['kind'] != 'close':
  return False,['公开子集仅支持月结工作簿。']
 perturb=close_scenarios(src)
 for key in g['workbook']['formula_cells']:
  sn,cell=key.split('!')
  if not actual.get(sn,{}).get('cells',{}).get(cell,{}).get('formula'):errors.append(key+'公式被替换为常量');continue
  for label,scenario in perturb:
   try:
    b=calc(ref,sn,cell,scenario)
   except Exception as e:
    errors.append('参考工作簿需核验：'+key+' ['+label+'] '+str(e));continue
   try:
    a=calc(actual,sn,cell,scenario)
    if is_number(a) and is_number(b):ok=abs(a-b)<=1e-6
    else:ok=type(a) is type(b) and a==b
    if not ok:errors.append(key+'在输入扰动下计算结果不符 ['+label+']')
   except UnsupportedFormula as e:errors.append('需人工核验等价公式：'+key+' '+str(e))
   except Exception as e:errors.append(key+'计算失败 ['+label+']：'+str(e))
 return not errors,sorted(set(errors))

def valid_ref(task,ref,manifest):
 if not isinstance(ref,str) or '#' not in ref:return False
 name,loc=ref.split('#',1);p=(task/name).resolve()
 if not p.is_relative_to(task.resolve()) or not p.is_file():return False
 if name.endswith('.csv'):
  with p.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.reader(f))
  return any(r and r[0]==loc for r in rows[1:])
 if name.endswith('.pdf'):
  return bool(re.fullmatch(r'p[1-9]\d*',loc)) and int(loc[1:])<=manifest.get('pdf_pages',{}).get(name,0)
 if name.endswith('.xlsx'):
  try:
   sn,cell=loc.split('!');b=xlsx(p)
   if sn not in b or not re.fullmatch(r'[A-Z]+\d+',cell):return False
   # Explicit blank source cells are valid evidence locations inside the sheet's used rectangle.
   maxr=max(int(re.search(r'\d+',c).group()) for c in b[sn]['cells']);maxc=max(colnum(re.match(r'[A-Z]+',c).group()) for c in b[sn]['cells'])
   return int(re.search(r'\d+',cell).group())<=maxr and colnum(re.match(r'[A-Z]+',cell).group())<=maxc
  except Exception:return False
 return False

def close_ratio_evidence(gold, answers, result, workbook_ok, has_duplicates):
    """Recognize the verified expense-total evidence chain for monthly close workbooks."""
    if not workbook_ok or has_duplicates or gold.get('workbook', {}).get('kind') != 'close':
        return set()
    expected = {item['item_id']: item for item in gold['items']}
    total_id, ratio_id = '月结!B2', '月结!B6'
    total, ratio = answers.get(total_id, {}), answers.get(ratio_id, {})
    total_gold = expected[total_id]
    if not equal(total.get('value'), total_gold['value']) or total.get('status') != total_gold['status']:
        return set()
    source = '原始材料/close.xlsx#'
    total_refs, ratio_refs = total.get('evidence_refs'), ratio.get('evidence_refs')
    for refs in (total_refs, ratio_refs):
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            return set()
    if source + total_id not in total_refs:
        return set()
    needed = {source + ratio_id, source + total_id, source + '月结!B5'}
    if not needed.issubset(ratio_refs):
        return set()
    # The caller already validated the workbook under unchanged and perturbed inputs.
    # Verify the actual dependency path as well. Other formula structures can be adjudicated.
    book = xlsx(result / 'corrected.xlsx')
    for key in (total_id, ratio_id):
        sheet, cell = key.split('!')
        actual = book[sheet]['cells'][cell]['formula']
        reference = gold['workbook']['formula_cells'][key].lstrip('=')
        if re.sub(r"[ $']", "", actual).upper() != re.sub(r"[ $']", "", reference).upper():
            return set()
    return set(total_refs).intersection(total_gold['evidence_refs'], expected[ratio_id]['evidence_refs'])


def score(test_root,gold_root,results,task_id):
 gold_dir=Path(gold_root)/'专家答案'/task_id;g=load(gold_dir/'gold.json');task=next(Path(test_root).glob('案例*/'+task_id));result=Path(results)/task_id
 details=[];fatal=[];critical=[];required_fact_errors=[];components={'scope':0,'values':0,'statuses':0,'evidence_locations':0,'delivery':0,'integrity':0}
 manifest=load(gold_dir/'input_manifest.json');unchanged=all((task/name).is_file() and sha(task/name)==h for name,h in manifest['sha256'].items())
 if not unchanged:fatal.append('SOURCE_CHANGED_OR_MISSING')
 try:r=load(result/'response.json')
 except Exception as e:return {'task_id':task_id,'auto_score':0,'max_auto_score':80,'final_score':None,'status':'交付失败','fatal_errors':fatal,'details':['response.json缺失或无法读取：'+str(e)]}
 if not isinstance(r,dict) or not isinstance(r.get('items'),list):return {'task_id':task_id,'auto_score':0,'max_auto_score':80,'final_score':None,'status':'交付失败','fatal_errors':fatal,'details':['response结构错误']}
 got={};dups=[]
 for item in r['items']:
  if not isinstance(item,dict) or not isinstance(item.get('item_id'),str):continue
  if item['item_id'] in got:dups.append(item['item_id'])
  got[item['item_id']]=item
 expected_ids={x['item_id'] for x in g['items']};extra=set(got)-expected_ids
 if dups:fatal.append('DUPLICATE_OUTPUT_IDS')
 scope=r.get('scope',{});components['scope']=10*sum(scope.get(k)==v for k,v in g['scope'].items())/len(g['scope']) if isinstance(scope,dict) and r.get('task_id')==task_id else 0
 workbook_ok,workbook_errors=workbook_check(g,task,result,gold_dir)
 inherited=close_ratio_evidence(g,got,result,workbook_ok,dups) if isinstance(g.get('workbook'),dict) else set()
 if inherited:
  total_refs=got['月结!B2']['evidence_refs']
  if not all(valid_ref(task,ref,manifest) for ref in total_refs):inherited=set()
 values=statuses=evidence=0
 for e in g['items']:
  a=got.get(e['item_id'],{});v=equal(e['value'],a.get('value')) and 'value' in a;s=a.get('status')==e['status'];values+=v;statuses+=s
  refs=a.get('evidence_refs',[]);refs=refs if isinstance(refs,list) and all(isinstance(q,str) for q in refs) else []
  bad=[q for q in refs if not valid_ref(task,q,manifest)]
  if bad:fatal.append('INVALID_EVIDENCE_REFERENCE');details.append({'item_id':e['item_id'],'invalid_refs':bad})
  covered=set(refs)
  if e['item_id']=='月结!B6' and inherited and not bad:
   additional=inherited-covered
   if additional:details.append({'item_id':e['item_id'],'evidence_via':'月结!B2','credited_refs':sorted(additional)})
   covered.update(inherited)
  coverage=len(set(e['evidence_refs'])&covered)/max(len(set(e['evidence_refs'])),1)
  evidence+=0 if bad else coverage
  if e['critical'] and (not v or not s):critical.append(e['item_id'])
  if e['status'] in ('REVIEW','INSUFFICIENT') and a.get('status') in ('MATCHED','CLEARED','DRAFT','SUPPORTED','COMPLETE','OK','BALANCED'):fatal.append('UNSUPPORTED_DEFINITE_CONCLUSION')
  if not v or not s:
   required_fact_errors.append(e['item_id']);details.append({'item_id':e['item_id'],'value_correct':v,'status_correct':s})
 penalty=len(g['items'])/(len(g['items'])+len(extra));n=len(g['items'])
 components['values']=30*values/n*penalty;components['statuses']=10*statuses/n*penalty;components['evidence_locations']=10*evidence/n
 readable_note=(result/'workpaper.md').is_file() and (result/'workpaper.md').stat().st_size>0
 components['delivery']=(5 if readable_note else 0)+(5 if workbook_ok and expected_ids<=set(got) else 0)
 components['integrity']=(5 if unchanged else 0)+(5 if not dups and not extra and r.get('task_id')==task_id else 0)
 if extra:details.append({'extra_items':sorted(extra)})
 if not readable_note:details.append('缺workpaper.md')
 details+=workbook_errors
 gate_errors=[]
 if required_fact_errors:gate_errors.append('REQUIRED_FACTS_INCORRECT')
 if components['scope']!=10:gate_errors.append('SCOPE_INCORRECT')
 if extra:gate_errors.append('EXTRA_OUTPUT_ITEMS')
 auto=round(sum(components.values()),4);manual=None;final=None;state='待人工评分'
 review=result/'manual_review.json'
 if review.exists():
  manual=load(review);a,b=manual.get('professional_reasoning'),manual.get('evidence_semantics')
  if all(isinstance(x,(int,float)) and not isinstance(x,bool) and 0<=x<=10 for x in (a,b)) and manual.get('reviewer') and manual.get('reviewed_at'):
   final=round(auto+a+b,4);fatal+=manual.get('fatal_errors',[]);state='通过' if final>=85 and not fatal and not critical and not gate_errors and workbook_ok and readable_note else '未通过'
  else:details.append('人工评分不完整或缺复核人/日期')
 if fatal or critical or gate_errors or not workbook_ok or not readable_note:state='未通过' if final is not None else '自动检查发现失败项，待人工复核'
 formula_review=[message for message in workbook_errors if message.startswith(('需人工核验等价公式：','参考工作簿需核验：'))]
 if formula_review:
  state='工作簿公式待核验，客观分暂计';final=None
 report={'task_id':task_id,'auto_score':auto,'max_auto_score':80,'manual_score':None if final is None else final-auto,'final_score':final,'status':state,'components':{k:round(v,4) for k,v in components.items()},'fatal_errors':sorted(set(fatal)),'critical_errors':critical,'required_fact_errors':required_fact_errors,'gate_errors':gate_errors,'details':details,'notes':'引用位置核验不等于引用语义正确；需人工判断。客观评分技术基线 v1.2 支持SUM、IF、ROUND、COUNT引用、ROWS引用及基本运算；超出支持范围的公式需人工核验。'}
 if formula_review:report['workbook_review_required']=formula_review
 return report

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--test-root',required=True);p.add_argument('--gold-root',required=True);p.add_argument('--results',required=True);p.add_argument('--task',required=True);p.add_argument('--output');a=p.parse_args();report=score(a.test_root,a.gold_root,a.results,a.task);s=json.dumps(report,ensure_ascii=False,indent=2);print(s)
 if a.output:Path(a.output).write_text(s+'\n',encoding='utf-8')

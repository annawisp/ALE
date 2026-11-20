"""Offline scorer. Python 3.10+ standard library only; never executes workbook macros."""
from pathlib import Path
from decimal import Decimal, InvalidOperation
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
    cells[c.attrib['r']]={'value':val,'formula':f.text if f is not None else None}
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
def addresses(a):
 a=a.replace('$','')
 if ':' not in a:return [a]
 start,end=a.split(':');c1,r1=re.fullmatch(r'([A-Z]+)(\d+)',start).groups();c2,r2=re.fullmatch(r'([A-Z]+)(\d+)',end).groups()
 return [colstr(c)+str(r) for r in range(int(r1),int(r2)+1) for c in range(colnum(c1),colnum(c2)+1)]

class UnsupportedFormula(Exception):pass
def calc(book,sheet,cell,overrides=None,stack=None):
 overrides=overrides or {};stack=set() if stack is None else set(stack);key=sheet+'!'+cell
 if key in overrides:return overrides[key]
 if key in stack:raise ValueError('Circular formula '+key)
 stack.add(key);data=book[sheet]['cells'].get(cell,{'value':None,'formula':None});f=data['formula']
 if not f:return '' if data['value'] is None else data['value']
 token=re.compile(r'"(?:[^"]|"")*"|(?:(?:\'[^\']+\'|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?')
 def repl(m):
  s=m.group()
  if s.startswith('"'):return repr(s[1:-1].replace('""','"'))
  sn,addr=s.rsplit('!',1) if '!' in s else (sheet,s);sn=sn.strip("'");return 'REF('+repr(sn)+','+repr(addr.replace('$',''))+')'
 expr=token.sub(repl,f.lstrip('='));expr=expr.replace('<>','!=').replace('^','**');expr=re.sub(r'(?<![<>=!])=(?!=)','==',expr)
 node=ast.parse(expr,mode='eval').body
 def ev(n):
  if isinstance(n,ast.Constant):return n.value
  if isinstance(n,ast.UnaryOp):return -ev(n.operand) if isinstance(n.op,ast.USub) else +ev(n.operand)
  if isinstance(n,ast.BinOp):
   a,b=ev(n.left),ev(n.right)
   if isinstance(n.op,ast.Add):return a+b
   if isinstance(n.op,ast.Sub):return a-b
   if isinstance(n.op,ast.Mult):return a*b
   if isinstance(n.op,ast.Div):return a/b
   if isinstance(n.op,ast.Pow):return a**b
  if isinstance(n,ast.Compare):
   a,b=ev(n.left),ev(n.comparators[0]);op=n.ops[0]
   if isinstance(op,ast.Eq):return a==b
   if isinstance(op,ast.NotEq):return a!=b
   if isinstance(op,ast.Gt):return a>b
   if isinstance(op,ast.GtE):return a>=b
   if isinstance(op,ast.Lt):return a<b
   if isinstance(op,ast.LtE):return a<=b
  if isinstance(n,ast.Call) and isinstance(n.func,ast.Name):
   name=n.func.id.upper()
   if name=='IF':return ev(n.args[1]) if ev(n.args[0]) else ev(n.args[2])
   if name=='REF':
    sn,addr=ev(n.args[0]),ev(n.args[1]);aa=addresses(addr);vals=[calc(book,sn,a,overrides,stack) for a in aa];return vals if ':' in addr else vals[0]
   if name=='SUM':
    def flat(x):return sum((flat(v) for v in x),0) if isinstance(x,list) else x if isinstance(x,(int,float)) else 0
    return sum(flat(ev(a)) for a in n.args)
   if name=='ROUND':return round(ev(n.args[0]),int(ev(n.args[1])))
   raise UnsupportedFormula(name)
  raise UnsupportedFormula(ast.dump(n))
 return ev(node)

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
 perturb=[{}, {'明细!C2':1327.25},{'明细!C7':0}]
 for key in g['workbook']['formula_cells']:
  sn,cell=key.split('!')
  if not actual.get(sn,{}).get('cells',{}).get(cell,{}).get('formula'):errors.append(key+'公式被替换为常量');continue
  for scenario in perturb:
   try:
    a,b=calc(actual,sn,cell,scenario),calc(ref,sn,cell,scenario)
    if isinstance(a,(float,int)) and isinstance(b,(float,int)):ok=abs(a-b)<=1e-6
    else:ok=a==b
    if not ok:errors.append(key+'在输入扰动下计算错误')
   except UnsupportedFormula as e:errors.append('需人工核验等价公式：'+key+' '+str(e))
   except Exception as e:errors.append(key+'计算失败：'+str(e))
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
 return {'task_id':task_id,'auto_score':auto,'max_auto_score':80,'manual_score':None if final is None else final-auto,'final_score':final,'status':state,'components':{k:round(v,4) for k,v in components.items()},'fatal_errors':sorted(set(fatal)),'critical_errors':critical,'required_fact_errors':required_fact_errors,'gate_errors':gate_errors,'details':details,'notes':'引用位置核验不等于引用语义正确；需人工判断。支持SUM/IF/ROUND及基本运算，其他等价公式需人工核验。'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--test-root',required=True);p.add_argument('--gold-root',required=True);p.add_argument('--results',required=True);p.add_argument('--task',required=True);p.add_argument('--output');a=p.parse_args();report=score(a.test_root,a.gold_root,a.results,a.task);s=json.dumps(report,ensure_ascii=False,indent=2);print(s)
 if a.output:Path(a.output).write_text(s+'\n',encoding='utf-8')

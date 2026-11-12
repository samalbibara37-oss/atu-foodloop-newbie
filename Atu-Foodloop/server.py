"""FoodLoop ATU: dependency-free WSGI application, Python 3.10+."""
import argparse
import calendar
import hashlib
import hmac
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
import traceback
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
LOCAL_TZ = timezone(timedelta(hours=5))
STATES = {'draft':'Черновик','submitted':'Отправлено','reviewing':'На проверке','returned':'Возвращено','accepted':'Принято'}
ORG_NAMES = ['Кафедра ТПП','Кафедра ТХиПП','Учебно-гостиничный комплекс ATU INN','Кофейня АТУ (ИП «Canella»)','Столовый комплекс корп. 1, 2 (ИП «Искандер»)']
ORG_WASTES = [[18,6,7],[8,12,15,16,10,11,4,17],[3,5,0,1,13,14],[9,14,2],[3,5,0,1,13,14]]
DEMO_PASSWORD = 'FoodLoop2026!'

def now(): return datetime.now(LOCAL_TZ).isoformat(timespec='seconds')
def today(): return datetime.now(LOCAL_TZ).date()
def month_shift(month, delta):
    y,m=map(int,month.split('-')); n=y*12+m-1+delta
    return f'{n//12:04d}-{n%12+1:02d}'
def hash_password(password, salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()
def verify_password(password, stored): return hmac.compare_digest(hash_password(password,stored.split(':')[0]),stored)
def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
def asdict(row): return dict(row) if row else None

class ApiError(Exception):
    def __init__(self,message,status=400,**extra): self.message,self.status,self.extra=message,status,extra

def require(value,message,status=400):
    if not value: raise ApiError(message,status)
def text(value,limit=200):
    require(isinstance(value,str),'Ожидается текст')
    value=value.strip(); require(0<len(value)<=limit,f'Введите от 1 до {limit} символов')
    require(not any(ord(c)<32 and c not in '\n\t' for c in value),'Недопустимые символы')
    return value

def number(value,minval=0,maxval=100000,optional=False):
    if optional and value in ('',None): return None
    try: n=float(value)
    except (TypeError,ValueError): raise ApiError('Ожидается число')
    require(math.isfinite(n) and minval<=n<=maxval,f'Число должно быть от {minval} до {maxval}')
    return n

def valid_month(value):
    require(isinstance(value,str) and bool(re.fullmatch(r'\d{4}-\d{2}',value)),'Период должен иметь вид ГГГГ-ММ')
    try: date.fromisoformat(value+'-01')
    except ValueError: raise ApiError('Некорректный месяц')
    require('2020-01'<=value<=today().strftime('%Y-%m'),'Недопустимый период')
    return value

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,address TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,login TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('operator','reviewer','admin','viewer')),org_id INTEGER REFERENCES organizations(id),active INTEGER NOT NULL DEFAULT 1,CHECK(role!='operator' OR org_id IS NOT NULL));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS methods(code TEXT PRIMARY KEY,name TEXT NOT NULL,factor REAL,rank INTEGER,note TEXT);
CREATE TABLE IF NOT EXISTS wastes(id INTEGER PRIMARY KEY,org_id INTEGER NOT NULL REFERENCES organizations(id),name TEXT NOT NULL,category TEXT NOT NULL,unit TEXT NOT NULL CHECK(unit IN ('кг','л')),recyclable TEXT NOT NULL,density REAL,ef_landfill REAL,ef_recommended REAL,recommended TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',source TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,UNIQUE(org_id,name));
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,org_id INTEGER NOT NULL REFERENCES organizations(id),month TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','reviewing','returned','accepted')),created_at TEXT NOT NULL,UNIQUE(org_id,month));
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY,report_id INTEGER NOT NULL REFERENCES reports(id),date TEXT NOT NULL,waste_id INTEGER NOT NULL REFERENCES wastes(id),quantity REAL NOT NULL CHECK(quantity>0),unit TEXT NOT NULL,mass_kg REAL,method TEXT NOT NULL REFERENCES methods(code),ef_base REAL,ef_actual REAL,factor_note TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),reason TEXT NOT NULL DEFAULT '',portions INTEGER,source TEXT NOT NULL DEFAULT 'manual',created_at TEXT NOT NULL,request_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,report_id INTEGER NOT NULL REFERENCES reports(id),actor_id INTEGER NOT NULL REFERENCES users(id),action TEXT NOT NULL,from_status TEXT,to_status TEXT,comment TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY,org_id INTEGER NOT NULL REFERENCES organizations(id),title TEXT NOT NULL,owner_id INTEGER NOT NULL REFERENCES users(id),due TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),result TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,completed_at TEXT);
CREATE INDEX IF NOT EXISTS entries_report ON entries(report_id);
CREATE INDEX IF NOT EXISTS reports_period ON reports(month,status);
CREATE TRIGGER IF NOT EXISTS entry_insert_lock BEFORE INSERT ON entries WHEN (SELECT status FROM reports WHERE id=NEW.report_id) NOT IN ('draft','returned') BEGIN SELECT RAISE(ABORT,'Report locked'); END;
CREATE TRIGGER IF NOT EXISTS entry_update_lock BEFORE UPDATE ON entries WHEN (SELECT status FROM reports WHERE id=OLD.report_id) NOT IN ('draft','returned') BEGIN SELECT RAISE(ABORT,'Report locked'); END;
CREATE TRIGGER IF NOT EXISTS entry_delete_lock BEFORE DELETE ON entries WHEN (SELECT status FROM reports WHERE id=OLD.report_id) NOT IN ('draft','returned') BEGIN SELECT RAISE(ABORT,'Report locked'); END;
'''

def xlsx_bytes(sheets):
    """Small standards-compliant XLSX writer. Strings are inline, never formulas."""
    def col(n):
        s=''
        while n: n,r=divmod(n-1,26);s=chr(65+r)+s
        return s
    def clean(s): return ''.join(c for c in str(s) if ord(c)>=32 or c in '\n\t\r')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        overrides=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+overrides+'</Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,rows) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml','<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF176B50"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" numFmtId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf><xf fontId="1" fillId="2" borderId="0" numFmtId="0" xfId="0" applyFill="1" applyFont="1"><alignment wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i,(name,rows) in enumerate(sheets,1):
            lines=[]
            for ri,row in enumerate(rows,1):
                cells=[]
                for ci,v in enumerate(row,1):
                    if v is None: continue
                    ref=f'{col(ci)}{ri}';style='1' if ri==1 else '0'
                    if isinstance(v,bool): v=int(v)
                    if isinstance(v,(int,float)) and math.isfinite(v): cells.append(f'<c r="{ref}" s="{style}"><v>{v}</v></c>')
                    else: cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(clean(v))}</t></is></c>')
                lines.append(f'<row r="{ri}">'+''.join(cells)+'</row>')
            z.writestr(f'xl/worksheets/sheet{i}.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="20" width="24" customWidth="1"/></cols><sheetData>'+''.join(lines)+'</sheetData></worksheet>')
    return out.getvalue()

class Application:
    def __init__(self,db_path=None,demo=False,admin_password=None):
        self.db_path=str(db_path or os.environ.get('FOODLOOP_DB',ROOT/'instance'/'foodloop.sqlite3'))
        Path(self.db_path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            from features import migrate
            migrate(db)
            if not db.execute('SELECT 1 FROM users').fetchone():
                require(demo or (admin_password and len(admin_password)>=12),'Для новой базы укажите --demo или ADMIN_PASSWORD (от 12 символов)')
                self.initialize(db,demo,admin_password)
        with self.connect() as db: self.demo=db.execute("SELECT value FROM settings WHERE key='demo'").fetchone()[0]=='1'

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.db_path,timeout=20);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db: yield db
        finally: db.close()

    def history(self,db,rid,uid,action,old=None,new=None,comment=''):
        db.execute('INSERT INTO history(report_id,actor_id,action,from_status,to_status,comment,created_at) VALUES(?,?,?,?,?,?,?)',(rid,uid,action,old,new,comment,now()))

    def initialize(self,db,demo,password):
        cat=json.loads((ROOT/'data/catalog.json').read_text(encoding='utf-8'))
        db.execute('INSERT INTO settings VALUES(?,?)',('demo',str(int(demo))))
        for m in cat['methods']:db.execute('INSERT INTO methods VALUES(?,?,?,?,?)',(m['code'],m['name'],m['factor'],m['rank'],m['note']))
        pwd=hash_password(DEMO_PASSWORD if demo else password)
        db.execute('INSERT INTO users(login,name,password_hash,role) VALUES(?,?,?,?)',('admin','Администратор',pwd,'admin'))
        if not demo:
            for idx,name in enumerate(ORG_NAMES):
                oid=db.execute('INSERT INTO organizations(name,kind,address) VALUES(?,?,?)',(name,['Лаборатория','Лаборатория','Общепит','Кофейня','Столовая'][idx],'АТУ, Алматы')).lastrowid
                for wi in ORG_WASTES[idx]:
                    w=cat['wastes'][wi]
                    db.execute('INSERT INTO wastes(org_id,name,category,unit,recyclable,density,ef_landfill,ef_recommended,recommended,note,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(oid,w['name'],w['category'],w['unit'],w['recyclable'],0.92 if w['unit']=='л' else None,w['ef_landfill'],w['ef_recommended'],w['recommended'],w['note'] or '',cat['source']))
            return
        db.execute('INSERT INTO users(login,name,password_hash,role) VALUES(?,?,?,?)',('reviewer','Проверяющий АТУ',pwd,'reviewer'))
        db.execute('INSERT INTO users(login,name,password_hash,role) VALUES(?,?,?,?)',('director','Руководство АТУ',pwd,'viewer'))
        import random
        rng=random.Random(42)
        for idx,name in enumerate(ORG_NAMES):
            oid=db.execute('INSERT INTO organizations(name,kind,address) VALUES(?,?,?)',(name,['Лаборатория','Лаборатория','Общепит','Кофейня','Столовая'][idx],'АТУ, Алматы')).lastrowid
            uid=db.execute('INSERT INTO users(login,name,password_hash,role,org_id) VALUES(?,?,?,?,?)',(['tpp','thpp','hotel','coffee','canteen'][idx],f'Ответственный — {name}',pwd,'operator',oid)).lastrowid
            for wi in ORG_WASTES[idx]:
                w=cat['wastes'][wi]
                db.execute('INSERT INTO wastes(org_id,name,category,unit,recyclable,density,ef_landfill,ef_recommended,recommended,note,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(oid,w['name'],w['category'],w['unit'],w['recyclable'],0.92 if w['unit']=='л' else None,w['ef_landfill'],w['ef_recommended'],w['recommended'],w['note'] or '',cat['source']))
            quarter_start=f'{today().year:04d}-{((today().month-1)//3)*3+1:02d}'
            months=sorted({month_shift(today().strftime('%Y-%m'),s) for s in [-3,-2,-1]} | {month_shift(quarter_start,s) for s in [-3,-2,-1]})
            for month in months:
                shift=-1 if month==month_shift(today().strftime('%Y-%m'),-1) else -2
                rid=db.execute('INSERT INTO reports(org_id,month,created_at) VALUES(?,?,?)',(oid,month,now())).lastrowid
                self.history(db,rid,uid,'created',None,'draft','Синтетические демонстрационные данные')
                for day in [3,8,13,18,23,27]:
                    for w in db.execute('SELECT * FROM wastes WHERE org_id=?',(oid,)).fetchall():
                        qty=round(rng.uniform(0.3,3) * (5 if idx==4 else 2 if idx==2 else 1),2)
                        if shift==-1 and idx==4 and 'нереализованной' in w['name']:qty*=1.8
                        method='TBO'
                        if rng.random()<0.24:method=self.allowed_methods(w)[1] if len(self.allowed_methods(w))>1 else 'TBO'
                        mass=qty if w['unit']=='кг' else qty*w['density']
                        factor=self.factor(w,method)
                        db.execute('INSERT INTO entries(report_id,date,waste_id,quantity,unit,mass_kg,method,ef_base,ef_actual,factor_note,user_id,reason,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(rid,f'{month}-{day:02d}',w['id'],round(qty,3),w['unit'],round(mass,6),method,w['ef_landfill'],factor,w['note'],uid,'Учебный набор','demo',now()))
                self.history(db,rid,uid,'submitted','draft','submitted','Демонстрационный переход')
                self.history(db,rid,2,'reviewing','submitted','reviewing','Демонстрационный переход')
                db.execute("UPDATE reports SET status='accepted' WHERE id=?",(rid,))
                self.history(db,rid,2,'accepted','reviewing','accepted','Синтетический отчёт для демонстрации')

    @staticmethod
    def allowed_methods(w):
        name=w['name'].lower();cat=w['category'];allowed=['TBO']
        if cat=='Органические':
            allowed+=['AD']
            if not any(x in name for x in ['мяс','кости','рыб','сахароза']):allowed+=['COMPOST']
            if any(x in name for x in ['овощ','фрукт','хлеб']):allowed+=['FEED']
            if 'нереализованной' in name:allowed+=['DONATE']
        elif cat=='Вторсырьё': allowed+=['RECYCLE']
        elif cat=='Специальные' and 'масло' in name:allowed+=['OIL']
        return allowed

    @staticmethod
    def factor(w,method):
        if method=='TBO':return w['ef_landfill']
        if method in ('DONATE','FEED'):return None  # no comparable treatment factor for prevented food
        if method in ('AD','COMPOST'):return 8.88 if w['ef_landfill'] is not None else None
        if method in ('RECYCLE','OIL'):return 6.41 if w['ef_landfill'] is not None else None
        return None

    def user(self,db,env):
        cookie=SimpleCookie()
        try:cookie.load(env.get('HTTP_COOKIE','')); token=cookie.get('foodloop_session'); token=token.value if token else ''
        except Exception:token=''
        row=db.execute('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',(token_hash(token),time.time())).fetchone()
        require(row,'Войдите в систему',401)
        if row['role']=='operator':require(db.execute('SELECT active FROM organizations WHERE id=?',(row['org_id'],)).fetchone()[0],'Организация отключена',403)
        return dict(row)

    @staticmethod
    def can_review(user):require(user['role'] in ('admin','reviewer'),'Недостаточно прав',403)
    @staticmethod
    def admin(user):require(user['role']=='admin','Требуются права администратора',403)
    @staticmethod
    def org_scope(user,oid):require(user['role']!='operator' or user['org_id']==oid,'Данные другой организации недоступны',403)
    def report(self,db,user,rid,write=False):
        r=db.execute('SELECT r.*,o.name org_name FROM reports r JOIN organizations o ON o.id=r.org_id WHERE r.id=?',(rid,)).fetchone()
        require(r,'Отчёт не найден',404);self.org_scope(user,r['org_id'])
        if write:
            require(user['role']=='operator','Записи вносит ответственное лицо',403)
            require(r['status']=='draft','Отчёт заблокирован. Для возврата нажмите «Начать исправление».',409)
        return r

    def __call__(self,env,start_response):
        status=200;headers=[]
        try:
            path=env.get('PATH_INFO','/');method=env['REQUEST_METHOD']
            if path.startswith('/api/'):
                require(method in ('GET','POST','PATCH','DELETE'),'Метод не поддерживается',405)
                params={k:v[0] for k,v in parse_qs(env.get('QUERY_STRING','')).items()}
                body={}
                if method!='GET':
                    origin=env.get('HTTP_ORIGIN')
                    require(not origin or urlsplit(origin).netloc==env.get('HTTP_HOST'),'Запрос с другого сайта запрещён',403)
                    require(env.get('CONTENT_TYPE','').split(';')[0]=='application/json','Нужен Content-Type application/json',415)
                    try:length=int(env.get('CONTENT_LENGTH') or 0)
                    except ValueError:raise ApiError('Некорректный размер запроса')
                    require(0<length<=800000,'Слишком большой или пустой запрос',413)
                    try:body=json.loads(env['wsgi.input'].read(length))
                    except (ValueError,UnicodeError):raise ApiError('Некорректный JSON')
                    require(isinstance(body,dict),'Ожидается JSON-объект')
                with self.connect() as db:
                    if method!='GET':db.execute('BEGIN IMMEDIATE')
                    public=path in ('/api/login','/api/access-options','/api/public-config') or (path=='/api/access-requests' and method=='POST')
                    user=None if public else self.user(db,env)
                    if user and method!='GET':require(hmac.compare_digest(env.get('HTTP_X_CSRF_TOKEN',''),user['csrf']),'Обновите страницу и повторите действие',403)
                    result=self.api(db,user,method,path,params,body,env,headers)
                if isinstance(result,bytes):
                    payload=result
                    if not any(k=='Content-Type' for k,v in headers):headers.append(('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
                else:payload=json.dumps(result,ensure_ascii=False,allow_nan=False).encode();headers.append(('Content-Type','application/json; charset=utf-8'))
            else:
                require(method in ('GET','HEAD'),'Метод не поддерживается',405)
                rel='index.html' if path=='/' else path.removeprefix('/static/') if path.startswith('/static/') else ''
                p=(ROOT/'static'/rel).resolve()
                require(rel and p.is_relative_to((ROOT/'static').resolve()) and p.is_file(),'Страница не найдена',404)
                payload=p.read_bytes();headers.append(('Content-Type',(mimetypes.guess_type(p)[0] or 'application/octet-stream')+('; charset=utf-8' if p.suffix in ('.html','.css','.js','.svg') else '')))
        except ApiError as e:
            status=e.status;payload=json.dumps({'error':e.message,**e.extra},ensure_ascii=False).encode();headers.append(('Content-Type','application/json; charset=utf-8'))
        except sqlite3.IntegrityError:
            status=409;payload=json.dumps({'error':'Запись уже существует или изменение нарушает целостность данных'},ensure_ascii=False).encode();headers.append(('Content-Type','application/json; charset=utf-8'))
        except Exception:
            traceback.print_exc();status=500;payload=b'{"error":"Internal server error"}';headers.append(('Content-Type','application/json'))
        headers += [('Content-Length',str(len(payload))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin'),('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")]
        from http import HTTPStatus
        start_response(f'{status} {HTTPStatus(status).phrase}',headers)
        return [b'' if env['REQUEST_METHOD']=='HEAD' else payload]

    def api(self,db,u,verb,path,q,b,env,headers):
        from features import extra_api, UNHANDLED
        extra=extra_api(self,db,u,verb,path,q,b,env,headers)
        if extra is not UNHANDLED:return extra
        if path=='/api/login' and verb=='POST':
            login=text(b.get('login',''),80).lower();password=text(b.get('password',''),200)
            key=token_hash(env.get('REMOTE_ADDR','')+'|'+login);attempt=db.execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
            require(not attempt or attempt['until']<time.time() or attempt['count']<10,'Слишком много попыток. Подождите 15 минут.',429)
            usr=db.execute('SELECT * FROM users WHERE login=? AND active=1',(login,)).fetchone()
            if not usr or not verify_password(password,usr['password_hash']):
                count=attempt['count']+1 if attempt and attempt['until']>time.time() else 1
                db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,count,time.time()+900));db.commit()
                raise ApiError('Неверный логин или пароль',401)
            if usr['role']=='operator':require(db.execute('SELECT active FROM organizations WHERE id=?',(usr['org_id'],)).fetchone()[0],'Организация отключена',403)
            db.execute('DELETE FROM login_attempts WHERE key=?',(key,));db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token_hash(token),usr['id'],csrf,time.time()+43200))
            secure='; Secure' if os.environ.get('COOKIE_SECURE')=='1' else ''
            headers.append(('Set-Cookie',f'foodloop_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200{secure}'))
            return {'ok':True,'csrf':csrf}
        if path=='/api/logout' and verb=='POST':
            db.execute('DELETE FROM sessions WHERE user_id=? AND csrf=?',(u['id'],u['csrf']));headers.append(('Set-Cookie','foodloop_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'));return {'ok':True}
        if path=='/api/me' and verb=='GET':return {k:u[k] for k in ('id','login','name','role','org_id','csrf')}|{'demo':self.demo,'today':today().isoformat()}
        if path=='/api/password' and verb=='POST':
            require(verify_password(b.get('old',''),u['password_hash']),'Текущий пароль неверен',403)
            pwd=text(b.get('new',''),200);require(len(pwd)>=12,'Новый пароль — от 12 символов')
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(hash_password(pwd),u['id']));db.execute('DELETE FROM sessions WHERE user_id=? AND csrf!=?',(u['id'],u['csrf']));return {'ok':True}
        if path=='/api/bootstrap' and verb=='GET':
            orgs=[dict(x) for x in db.execute('SELECT * FROM organizations ORDER BY id') if u['role']!='operator' or x['id']==u['org_id']]
            wastes=[]
            for x in db.execute('SELECT * FROM wastes ORDER BY name'):
                if u['role']=='operator' and x['org_id']!=u['org_id']:continue
                wastes.append(dict(x)|{'allowed_methods':self.allowed_methods(x),'default_method':x['default_method']})
            return {'organizations':orgs,'wastes':wastes,'methods':[dict(x) for x in db.execute('SELECT * FROM methods ORDER BY rank')],'states':STATES}
        if path=='/api/organizations' and verb=='POST':
            self.admin(u);oid=db.execute('INSERT INTO organizations(name,kind,address) VALUES(?,?,?)',(text(b.get('name','')),text(b.get('kind','')),str(b.get('address',''))[:200])).lastrowid
            from features import audit
            audit(db,u,'organization_created','organization',oid,{'name':b.get('name')})
            return {'id':oid}
        if path=='/api/users':
            self.admin(u)
            if verb=='GET':return [dict(x) for x in db.execute('SELECT id,login,name,role,org_id,active FROM users')]
            if verb=='POST':
                login=text(b.get('login',''),80).lower();require(bool(re.fullmatch(r'[a-z0-9_.@-]{3,80}',login)),'Логин: от 3 латинских букв, цифр, точек или подчёркиваний')
                role=b.get('role');require(role in ('operator','admin','reviewer','viewer'),'Неизвестная роль')
                pwd=text(b.get('password',''));require(len(pwd)>=12,'Пароль — от 12 символов')
                oid=b.get('org_id') if role=='operator' else None
                if role=='operator':require(db.execute('SELECT id FROM organizations WHERE id=? AND active=1',(oid,)).fetchone(),'Выберите организацию')
                uid=db.execute('INSERT INTO users(login,name,password_hash,role,org_id) VALUES(?,?,?,?,?)',(login,text(b.get('name','')),hash_password(pwd),role,oid)).lastrowid
                db.execute('UPDATE users SET email=? WHERE id=?',(str(b.get('email',''))[:200],uid))
                from features import audit
                audit(db,u,'user_created','user',uid,{'login':login,'role':role,'org_id':oid})
                return {'id':uid}
        if path=='/api/wastes' and verb=='POST':
            require(u['role'] in ('operator','reviewer','admin'),'Недостаточно прав',403)
            oid=u['org_id'] if u['role']=='operator' else b.get('org_id');require(db.execute('SELECT 1 FROM organizations WHERE id=?',(oid,)).fetchone(),'Выберите организацию')
            unit=b.get('unit','кг');require(unit in ('кг','л'),'Единица: кг или л')
            category=b.get('category','Смешанные');require(category in ('Органические','Вторсырьё','Специальные','Смешанные'),'Некорректная категория')
            rec=b.get('recyclable','Нет');require(rec in ('Да','Частично','Нет'),'Некорректный признак переработки')
            density=number(b.get('density'),0.001,20,True) if unit=='л' else None
            wid=db.execute('INSERT INTO wastes(org_id,name,category,unit,recyclable,density,recommended,note,source) VALUES(?,?,?,?,?,?,?,?,?)',(oid,text(b.get('name','')),category,unit,rec,density,'Уточнить у принимающей организации',str(b.get('note',''))[:1000],'Добавлен пользователем; коэффициент CO₂ не задан')).lastrowid
            if u['role']!='admin':db.execute('UPDATE wastes SET approved=0,proposed_by=? WHERE id=?',(u['id'],wid))
            from features import audit
            audit(db,u,'waste_proposed','waste',wid,{'name':b.get('name')})
            return {'id':wid}
        match=re.fullmatch(r'/api/wastes/(\d+)',path)
        if match and verb=='PATCH':
            self.admin(u);wid=int(match[1]);require(db.execute('SELECT id FROM wastes WHERE id=?',(wid,)).fetchone(),'Вид не найден',404)
            db.execute('UPDATE wastes SET active=? WHERE id=?',(1 if b.get('active') else 0,wid));return {'ok':True}
        if path=='/api/reports':
            if verb=='GET':
                rows=db.execute('SELECT r.*,o.name org_name,(SELECT count(*) FROM entries e WHERE e.report_id=r.id) entry_count,(SELECT comment FROM history h WHERE h.report_id=r.id ORDER BY h.id DESC LIMIT 1) last_comment FROM reports r JOIN organizations o ON o.id=r.org_id ORDER BY r.month DESC,r.id DESC').fetchall()
                return [dict(x) for x in rows if (u['role']!='operator' or x['org_id']==u['org_id']) and (not q.get('month') or x['month']==q['month'])]
            if verb=='POST':
                require(u['role']=='operator','Отчёт создаёт ответственное лицо',403);month=valid_month(b.get('month'))
                db.execute('INSERT OR IGNORE INTO reports(org_id,month,created_at) VALUES(?,?,?)',(u['org_id'],month,now()))
                r=db.execute('SELECT * FROM reports WHERE org_id=? AND month=?',(u['org_id'],month)).fetchone()
                if not db.execute('SELECT 1 FROM history WHERE report_id=?',(r['id'],)).fetchone():self.history(db,r['id'],u['id'],'created',None,'draft')
                return dict(r)
        match=re.fullmatch(r'/api/reports/(\d+)(?:/(transition|entries))?',path)
        if match:
            rid=int(match[1]);sub=match[2];r=self.report(db,u,rid,write=sub=='entries' and verb=='POST')
            if verb=='GET' and sub is None:return dict(r)|{'entries':self.entries(db,u,report_id=rid),'history':[dict(x) for x in db.execute('SELECT h.*,u.name actor FROM history h JOIN users u ON u.id=h.actor_id WHERE report_id=? ORDER BY h.id',(rid,))]}
            if sub=='transition' and verb=='POST':
                target=b.get('status');old=r['status'];comment=str(b.get('comment','')).strip()[:1000]
                if target=='submitted':
                    require(u['role']=='operator','Отправляет ответственное лицо',403);require(old=='draft','Начните исправление возвращённого отчёта',409)
                    require(db.execute('SELECT 1 FROM entries WHERE report_id=?',(rid,)).fetchone(),'Добавьте хотя бы одну запись')
                elif target=='draft':
                    require(u['role']=='operator' and old=='returned','Начать исправление может ответственное лицо после возврата',403)
                else:
                    self.can_review(u)
                    if target=='accepted':
                        pending=db.execute('SELECT count(*) FROM entries e JOIN wastes w ON w.id=e.waste_id WHERE e.report_id=? AND w.approved=0',(rid,)).fetchone()[0]
                        require(not pending,'Сначала администратор должен утвердить новые виды отходов',409)
                        from features import validate_snapshots
                        validate_snapshots(self,db,rid)
                    require((old,target) in [('submitted','reviewing'),('reviewing','returned'),('reviewing','accepted')],'Недопустимый переход статуса',409)
                    if target=='returned':require(comment,'При возврате обязательно напишите комментарий')
                if target=='submitted':db.execute('UPDATE reports SET submitted_at=? WHERE id=?',(now(),rid))
                if target=='reviewing':db.execute('UPDATE reports SET reviewer_id=? WHERE id=?',(u['id'],rid))
                if target in ('returned','accepted'):db.execute('UPDATE reports SET decided_at=? WHERE id=?',(now(),rid))
                db.execute('UPDATE reports SET status=? WHERE id=?',(target,rid));self.history(db,rid,u['id'],target,old,target,comment)
                return {'status':target}
            if sub=='entries' and verb=='POST':return self.save_entry(db,u,r,b)
        match=re.fullmatch(r'/api/entries/(\d+)',path)
        if match and verb in ('PATCH','DELETE'):
            eid=int(match[1]);entry=db.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone();require(entry,'Запись не найдена',404)
            r=self.report(db,u,entry['report_id'],write=True)
            if verb=='DELETE':
                db.execute('DELETE FROM entries WHERE id=?',(eid,));self.history(db,r['id'],u['id'],'entry_deleted',comment=json.dumps(dict(entry),ensure_ascii=False));return {'ok':True}
            return self.save_entry(db,u,r,b,eid,entry)
        if path=='/api/registry' and verb=='GET':return self.entries(db,u,q=q)
        if path=='/api/analytics' and verb=='GET':return self.analytics(db,u,q)
        if path=='/api/missing' and verb=='GET':
            month=valid_month(q.get('month',today().strftime('%Y-%m')))
            rows=db.execute("SELECT o.id,o.name,r.status,r.id report_id FROM organizations o LEFT JOIN reports r ON r.org_id=o.id AND r.month=? WHERE o.active=1 AND (r.status IS NULL OR r.status!='accepted')",(month,)).fetchall()
            return [dict(x) for x in rows if u['role']!='operator' or x['id']==u['org_id']]
        if path=='/api/actions':
            if verb=='GET':return [dict(x) for x in db.execute('SELECT a.*,u.name owner,o.name org_name FROM actions a JOIN users u ON u.id=a.owner_id JOIN organizations o ON o.id=a.org_id ORDER BY a.id DESC') if u['role']!='operator' or x['org_id']==u['org_id']]
            if verb=='POST':
                require(u['role']!='viewer','Недостаточно прав',403);oid=u['org_id'] if u['role']=='operator' else b.get('org_id')
                require(db.execute('SELECT id FROM organizations WHERE id=?',(oid,)).fetchone(),'Выберите организацию')
                due=b.get('due',today().isoformat())
                try:date.fromisoformat(due)
                except (ValueError,TypeError):raise ApiError('Некорректная дата')
                aid=db.execute('INSERT INTO actions(org_id,title,owner_id,due,created_at) VALUES(?,?,?,?,?)',(oid,text(b.get('title',''),500),u['id'],due,now())).lastrowid
                return {'id':aid}
        match=re.fullmatch(r'/api/actions/(\d+)',path)
        if match and verb=='PATCH':
            require(u['role']!='viewer','Недостаточно прав',403);a=db.execute('SELECT * FROM actions WHERE id=?',(int(match[1]),)).fetchone();require(a,'Действие не найдено',404);self.org_scope(u,a['org_id'])
            require(a['status']=='open','Действие уже завершено',409);result=text(b.get('result',''),1000)
            db.execute("UPDATE actions SET status='done',result=?,completed_at=? WHERE id=?",(result,now(),a['id']));return {'ok':True}
        if path=='/api/scale' and verb=='POST':
            require(u['role']=='operator','Весы доступны ответственному лицу',403)
            return {'quantity':round(0.1+secrets.randbelow(1990)/100,2),'unit':'кг','source':'simulator','timestamp':now(),'device':'DEMO-SCALE-01','notice':'Программная имитация. Не реальное измерение.'}
        if path=='/api/export' and verb=='GET':
            require(u['role'] in ('admin','reviewer','viewer'),'Сводную выгрузку формирует проверяющий или руководство',403)
            rows=self.entries(db,u,q=q);groups=defaultdict(float)
            for e in rows:groups[(e['org_name'],e['waste_name'],e['unit'],e['method_name'])]+=e['quantity']
            summary=[['Организация','Вид отхода','Единица','Способ обращения','Количество']]+[[*k,round(v,3)] for k,v in sorted(groups.items())]
            detail=[['Дата','Организация','Отход','Количество','Единица','Масса кг','Способ','Ответственный','Источник','Расчёт CO₂ кг','Предотвращённая еда','Примечание коэффициентов']]
            for e in rows:detail.append([e['date'],e['org_name'],e['waste_name'],e['quantity'],e['unit'],e['mass_kg'],e['method_name'],e['user_name'],e['source'],e['co2_actual'],e['method']=='DONATE',e['factor_note']])
            notes=[['Параметр','Значение'],['Набор','Синтетические демо-данные' if self.demo else 'Рабочая база'],['Диапазон',f"{q.get('from','все')} — {q.get('to','все')}"],['Статусы','Только принятые отчёты'],['CO₂','Оценка по предоставленному справочнику, DEFRA 2024; не проверенный эффект в Казахстане'],['Масло','1 л = 0,92 кг (допущение справочника)'],['Фудшеринг','Показан отдельно; не включён в массу образовавшихся отходов и расчёт CO₂'],['Пропуски','Неизвестная плотность / коэффициент: пустое значение, не ноль']]
            headers.append(('Content-Disposition','attachment; filename="foodloop-report.xlsx"'))
            from features import ledger_sheets
            return xlsx_bytes([('Сводный отчёт',summary),('Реестр',detail),('Методика',notes)]+ledger_sheets(db,rows))
        raise ApiError('Маршрут не найден',404)

    def save_entry(self,db,u,r,b,eid=None,old=None):
        w=db.execute('SELECT * FROM wastes WHERE id=? AND org_id=? AND active=1',(b.get('waste_id'),r['org_id'])).fetchone();require(w,'Выберите действующий вид отходов своей организации')
        value=number(b.get('quantity'),0.001,10000)
        try:dt=date.fromisoformat(b.get('date',''))
        except (ValueError,TypeError):raise ApiError('Некорректная дата')
        require(dt<=today() and dt.strftime('%Y-%m')==r['month'],'Дата должна быть внутри месяца отчёта и не в будущем')
        method=b.get('method');require(method in self.allowed_methods(w),'Этот способ не подходит виду отходов. Смешанную упаковку сначала разделите.')
        if method in ('DONATE','FEED'):require(b.get('safe_confirm') is True,'Подтвердите проверку условий безопасной передачи')
        duplicate=db.execute('SELECT id FROM entries WHERE report_id=? AND date=? AND waste_id=? AND quantity=? AND method=? AND id!=?',(r['id'],dt.isoformat(),w['id'],value,method,eid or -1)).fetchone()
        req=b.get('request_id') or secrets.token_hex(16);require(isinstance(req,str) and len(req)<=100,'Некорректный идентификатор')
        if not eid:
            previous=db.execute('SELECT * FROM entries WHERE request_id=?',(req,)).fetchone()
            if previous:
                require(previous['user_id']==u['id'] and previous['report_id']==r['id'],'Повторный идентификатор другого запроса',409)
                require(previous['waste_id']==w['id'] and previous['quantity']==value and previous['date']==dt.isoformat() and previous['method']==method,'Идентификатор уже использован для другого содержимого. Обновите форму.',409)
                return {'id':previous['id'],'replayed':True}
        if duplicate and not b.get('confirm_duplicate'):raise ApiError('Похожее взвешивание уже есть. Сохранить ещё одно?',409,duplicate=True)
        mass=value if w['unit']=='кг' else value*w['density'] if w['density'] else None
        portions=number(b.get('portions'),1,100000,True)
        if portions is not None:require(portions.is_integer(),'Количество порций должно быть целым');portions=int(portions)
        reason=str(b.get('reason',''))[:500];source=b.get('source','manual');require(source in ('manual','simulator'),'Неизвестный источник')
        if source=='simulator':require(w['unit']=='кг','Имитатор весов передаёт только килограммы')
        vals=(dt.isoformat(),w['id'],value,w['unit'],round(mass,6) if mass is not None else None,method,w['ef_landfill'],self.factor(w,method),w['note']+'; '+w['source'],u['id'],reason,portions,source)
        if eid:
            db.execute('UPDATE entries SET date=?,waste_id=?,quantity=?,unit=?,mass_kg=?,method=?,ef_base=?,ef_actual=?,factor_note=?,user_id=?,reason=?,portions=?,source=? WHERE id=?',(*vals,eid))
            self.history(db,r['id'],u['id'],'entry_updated',comment=json.dumps({'before':dict(old),'after':b},ensure_ascii=False))
        else:
            eid=db.execute('INSERT INTO entries(report_id,date,waste_id,quantity,unit,mass_kg,method,ef_base,ef_actual,factor_note,user_id,reason,portions,source,created_at,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(r['id'],*vals,now(),req)).lastrowid
            self.history(db,r['id'],u['id'],'entry_added',comment=f'Запись #{eid}: {w["name"]}, {value} {w["unit"]}')
        return {'id':eid}

    def entries(self,db,u,q=None,report_id=None):
        q=q or {};where=[];args=[]
        if report_id:where.append('r.id=?');args.append(report_id)
        else:where.append("r.status='accepted'")
        if u['role']=='operator':where.append('r.org_id=?');args.append(u['org_id'])
        for key,col in [('org_id','r.org_id'),('waste_id','e.waste_id'),('waste_name','w.name'),('method','e.method')]:
            if q.get(key):where.append(col+'=?');args.append(q[key])
        for key,op in [('from','>='),('to','<=')]:
            if q.get(key):
                try:date.fromisoformat(q[key])
                except ValueError:raise ApiError('Некорректный диапазон дат')
                where.append('e.date'+op+'?');args.append(q[key])
        require(not (q.get('from') and q.get('to')) or q['from']<=q['to'],'Начало периода позже конца')
        sql='SELECT e.*,w.name waste_name,w.category,w.recyclable,w.recommended,r.org_id,r.month,r.status,o.name org_name,m.name method_name,u.name user_name FROM entries e JOIN reports r ON r.id=e.report_id JOIN wastes w ON w.id=e.waste_id JOIN organizations o ON o.id=r.org_id JOIN methods m ON m.code=e.method JOIN users u ON u.id=e.user_id WHERE '+' AND '.join(where)+' ORDER BY e.date DESC,e.id DESC'
        out=[]
        for row in db.execute(sql,args):
            e=dict(row);mass=e['mass_kg'];known=mass is not None and e['ef_actual'] is not None and e['ef_base'] is not None
            e['co2_actual']=round(mass/1000*e['ef_actual'],6) if known else None
            e['co2_saving']=round(mass/1000*(e['ef_base']-e['ef_actual']),6) if known else None
            e['has_photo']=bool(db.execute('SELECT 1 FROM photos WHERE entry_id=?',(e['id'],)).fetchone())
            out.append(e)
        return out

    def analytics(self,db,u,q):
        rows=self.entries(db,u,q);waste=[e for e in rows if e['method']!='DONATE'];donated=[e for e in rows if e['method']=='DONATE']
        total=sum(e['mass_kg'] or 0 for e in waste);recycled=sum(e['mass_kg'] or 0 for e in waste if e['method'] in ('AD','COMPOST','RECYCLE','OIL'))
        by_month=defaultdict(float);by_org=defaultdict(float);by_kind=defaultdict(float)
        for e in waste:
            by_month[e['month']]+=e['mass_kg'] or 0;by_org[e['org_name']]+=e['mass_kg'] or 0;by_kind[e['waste_name']]+=e['mass_kg'] or 0
        monthly=sorted(by_month.items());change=None
        if len(monthly)>1 and monthly[-2][1]>0:change=(monthly[-1][1]/monthly[-2][1]-1)*100
        recommendations=[]
        unsold=sum(e['mass_kg'] or 0 for e in waste if 'нереализованной' in e['waste_name'])
        organic_tbo=sum(e['mass_kg'] or 0 for e in waste if e['category']=='Органические' and e['method']=='TBO')
        if unsold:recommendations.append({'title':'Проверить объём приготовления','evidence':f'{unsold:.1f} кг непроданной продукции за выбранный период.','action':'Сопоставить остатки с числом проданных порций; провести недельный пилот приготовления меньшими партиями.','kind':'prevention'})
        if organic_tbo:recommendations.append({'title':'Организовать отдельный сбор органики','evidence':f'{organic_tbo:.1f} кг органики отмечено как вывоз ТБО.','action':'Уточнить приём у переработчика и поставить отдельную тару. Изменить способ только после фактической передачи.','kind':'sorting'})
        # Anomalies compare a complete month to previous complete month for the same organization and type.
        series=defaultdict(lambda:defaultdict(float))
        for e in waste:series[(e['org_name'],e['waste_name'])][e['month']]+=e['mass_kg'] or 0
        for (org,name),values in series.items():
            months=sorted(m for m in values if m<today().strftime('%Y-%m') and (not q.get('from') or m+'-01'>=q['from']) and (not q.get('to') or m+f'-{calendar.monthrange(int(m[:4]),int(m[5:]))[1]:02d}'<=q['to']))
            if len(months)>=2 and month_shift(months[-1],-1)==months[-2]:
                a,c=values[months[-2]],values[months[-1]]
                if a>0 and c/a>1.3:recommendations.append({'title':'Рост отходов более 30%','evidence':f'{org}: {name}, {a:.1f} → {c:.1f} кг ({months[-2]} → {months[-1]}).','action':'Проверить полноту учёта, меню и посещаемость; зафиксировать причину роста.','kind':'anomaly'})
        def pairs(d):return [{'name':k,'value':round(v,3)} for k,v in sorted(d.items())]
        return {'mass_kg':round(total,3),'donated_kg':round(sum(e['mass_kg'] or 0 for e in donated),3),'recycled_kg':round(recycled,3),'recycled_percent':round(100*recycled/total,1) if total else 0,'eligible_kg':round(sum(e['mass_kg'] or 0 for e in waste if e['recyclable']=='Да'),3),'conditional_kg':round(sum(e['mass_kg'] or 0 for e in waste if e['recyclable']=='Частично'),3),'unknown_mass_records':sum(e['mass_kg'] is None for e in waste),'unknown_co2_records':sum(e['co2_actual'] is None for e in waste),'co2_kg':round(sum(e['co2_actual'] or 0 for e in waste),3),'co2_saving_kg':round(sum(e['co2_saving'] or 0 for e in waste),3),'records':len(rows),'months':pairs(by_month),'organizations':pairs(by_org),'kinds':pairs(by_kind),'change_percent':round(change,1) if change is not None else None,'recommendations':recommendations,'demo':self.demo}

def main():
    parser=argparse.ArgumentParser(description='FoodLoop ATU')
    parser.add_argument('--demo',action='store_true',help='Создать демонстрационную базу, если база пустая')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8000')))
    parser.add_argument('--strictPort',action='store_true',help=argparse.SUPPRESS);parser.add_argument('--db');parser.add_argument('--init-only',action='store_true');parser.add_argument('--production',action='store_true')
    args=parser.parse_args()
    try:app=Application(args.db,args.demo,os.environ.get('ADMIN_PASSWORD'))
    except ApiError as e:parser.error(e.message)
    if args.init_only:print('База готова.');return
    print(f'FoodLoop ATU: http://127.0.0.1:{args.port}',flush=True)
    if args.host=='0.0.0.0':
        print('Смартфон: одна доверенная сеть. Найдите IPv4 активного Wi-Fi командой ipconfig.',flush=True)
        print('Ниже КАНДИДАТЫ всех адаптеров, включая виртуальные. Не каждый доступен телефону:',flush=True)
        try:
            for addr in sorted(set(socket.gethostbyname_ex(socket.gethostname())[2])):
                if not addr.startswith('127.'):print(f'  http://{addr}:{args.port}',flush=True)
        except OSError:pass
    if app.demo:print('DEMO: canteen / reviewer / admin; пароль FoodLoop2026!',flush=True)
    if args.production:
        try:from waitress import serve
        except ImportError:parser.error('Установите зависимости: python -m pip install -r requirements.txt')
        serve(app,host=args.host,port=args.port,threads=8,max_request_body_size=800000)
    else:
        from wsgiref.simple_server import make_server,WSGIServer
        from socketserver import ThreadingMixIn
        class ThreadedServer(ThreadingMixIn,WSGIServer): daemon_threads=True
        print('Локальный демонстрационный сервер. Для публикации используйте --production и HTTPS.',flush=True)
        try:
            with make_server(args.host,args.port,app,server_class=ThreadedServer) as server:server.serve_forever()
        except KeyboardInterrupt:print('\nОстановлено.')

if __name__=='__main__':main()

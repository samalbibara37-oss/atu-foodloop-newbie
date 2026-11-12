"""Access governance, catalog approval, period comparison, photos and QR. No external services."""
import base64,calendar,importlib,ipaddress,json,re,secrets,sys,time
from pathlib import Path
from datetime import date,timedelta,datetime,timezone
from urllib.parse import urlsplit,urlencode
UNHANDLED=object()

def stamp():return datetime.now(timezone(timedelta(hours=5))).isoformat(timespec='seconds')
def migrate(db):
    additions={'wastes':{'approved':'INTEGER NOT NULL DEFAULT 1','proposed_by':'INTEGER REFERENCES users(id)','default_method':"TEXT NOT NULL DEFAULT 'TBO'"},'users':{'email':"TEXT NOT NULL DEFAULT ''"},'reports':{'submitted_at':'TEXT','reviewer_id':'INTEGER REFERENCES users(id)','decided_at':'TEXT'}}
    for table,columns in additions.items():
        existing={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
        for name,kind in columns.items():
            if name not in existing:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
                if table=='wastes' and name=='approved':db.execute("UPDATE wastes SET approved=0 WHERE source LIKE 'Добавлен пользователем%'")
    db.executescript('''
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor_id INTEGER REFERENCES users(id),action TEXT NOT NULL,entity TEXT NOT NULL,entity_id INTEGER,details TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_requests(id INTEGER PRIMARY KEY,login TEXT NOT NULL,name TEXT NOT NULL,email TEXT NOT NULL,org_id INTEGER NOT NULL REFERENCES organizations(id),password_hash TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),comment TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,decided_by INTEGER REFERENCES users(id));
CREATE UNIQUE INDEX IF NOT EXISTS pending_access_login ON access_requests(login) WHERE status='pending';
CREATE TABLE IF NOT EXISTS photos(entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,mime TEXT NOT NULL,data BLOB NOT NULL,actor_id INTEGER REFERENCES users(id),created_at TEXT NOT NULL);
''')
    for op,ref in [('insert','NEW'),('update','OLD'),('delete','OLD')]:
        db.execute('DROP TRIGGER IF EXISTS entry_'+op+'_lock')
        db.execute(f"CREATE TRIGGER entry_{op}_lock BEFORE {op.upper()} ON entries WHEN (SELECT status FROM reports WHERE id={ref}.report_id)!='draft' BEGIN SELECT RAISE(ABORT,'Report locked'); END")
        db.execute(f"CREATE TRIGGER IF NOT EXISTS photo_{op}_lock BEFORE {op.upper()} ON photos WHEN (SELECT r.status FROM entries e JOIN reports r ON r.id=e.report_id WHERE e.id={ref}.entry_id)!='draft' BEGIN SELECT RAISE(ABORT,'Report locked'); END")

def audit(db,u,action,entity,eid,details):
    db.execute('INSERT INTO audit(actor_id,action,entity,entity_id,details,created_at) VALUES(?,?,?,?,?,?)',(u['id'] if u else None,action,entity,eid,json.dumps(details,ensure_ascii=False),stamp()))

def ledger_sheets(db,rows):
    result=[]
    for i,o in enumerate(db.execute('SELECT * FROM organizations ORDER BY id'),1):
        records=[e for e in rows if e['org_id']==o['id']]
        if not records:continue
        body=[['Дата','Наименование отходов','Количество','Единица','Способ переработки / утилизации','Ответственное лицо']]
        body += [[e['date'],e['waste_name'],e['quantity'],e['unit'],e['method_name'],e['user_name']] for e in records]
        groups={}
        for e in records:
            k=(e['waste_name'],e['unit']);groups[k]=groups.get(k,0)+e['quantity']
        body += [['Итоги за выбранный период']]+[['',name,round(qty,3),unit] for (name,unit),qty in sorted(groups.items())]
        if o['name']=='Кафедра ТПП':body += [['Итого «тара + кости»','',round(sum(e['quantity'] for e in records if e['unit']=='кг'),3),'кг']]
        result.append((f'Организация {i}',[['Организация',o['name']]]+body))
    return result

def extra_api(app,db,u,verb,path,q,b,env,headers):
    S=importlib.import_module(app.__class__.__module__);require=S.require;text=S.text;number=S.number
    if path=='/api/public-config' and verb=='GET':return {'demo':app.demo,'access_request':True}
    if path=='/api/access-options' and verb=='GET':return [dict(r) for r in db.execute('SELECT id,name FROM organizations WHERE active=1 ORDER BY name')]
    if path=='/api/access-requests' and verb=='POST':
        key='request:'+S.token_hash(env.get('REMOTE_ADDR',''));hit=db.execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
        require(not hit or hit['until']<time.time() or hit['count']<8,'Слишком много заявок. Повторите через 15 минут.',429)
        count=hit['count']+1 if hit and hit['until']>time.time() else 1
        db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,count,time.time()+900));db.commit();db.execute('BEGIN IMMEDIATE')
        login=text(b.get('login',''),80).lower();require(re.fullmatch(r'[a-z0-9_.@-]{3,80}',login),'Логин: от 3 латинских букв, цифр, точек или подчёркиваний')
        pwd=text(b.get('password',''),200);require(len(pwd)>=12,'Пароль — от 12 символов')
        email=text(b.get('email',''),200);require(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email),'Укажите служебный e-mail')
        oid=b.get('org_id');require(db.execute('SELECT 1 FROM organizations WHERE id=? AND active=1',(oid,)).fetchone(),'Выберите действующую организацию')
        # Server always assigns operator. Client-supplied role/active/approval fields are ignored.
        require(not db.execute('SELECT 1 FROM users WHERE login=?',(login,)).fetchone(),'Логин занят')
        rid=db.execute('INSERT INTO access_requests(login,name,email,org_id,password_hash,created_at) VALUES(?,?,?,?,?,?)',(login,text(b.get('name','')),email,oid,S.hash_password(pwd),S.now())).lastrowid
        audit(db,None,'access_requested','access_request',rid,{'login':login,'org_id':oid})
        return {'ok':True,'message':'Заявка отправлена. Вход станет доступен после одобрения администратором.'}
    if path=='/api/access-requests' and verb=='GET':
        app.admin(u);return [dict(r) for r in db.execute('SELECT a.id,a.login,a.name,a.email,a.org_id,o.name org_name,a.status,a.comment,a.created_at FROM access_requests a JOIN organizations o ON o.id=a.org_id ORDER BY a.id DESC')]
    m=re.fullmatch(r'/api/access-requests/(\d+)',path)
    if m and verb=='PATCH':
        app.admin(u);rid=int(m[1]);a=db.execute('SELECT * FROM access_requests WHERE id=?',(rid,)).fetchone();require(a and a['status']=='pending','Заявка уже обработана или отсутствует',409)
        decision=b.get('status');require(decision in ('approved','rejected'),'Выберите решение');comment=str(b.get('comment','')).strip()[:1000]
        if decision=='approved':
            require(db.execute('SELECT active FROM organizations WHERE id=?',(a['org_id'],)).fetchone()[0],'Организация отключена')
            db.execute("INSERT INTO users(login,name,email,password_hash,role,org_id) VALUES(?,?,?,?,'operator',?)",(a['login'],a['name'],a['email'],a['password_hash'],a['org_id']))
        else:require(comment,'Укажите причину отказа')
        db.execute('UPDATE access_requests SET status=?,comment=?,decided_by=? WHERE id=?',(decision,comment,u['id'],rid));audit(db,u,'access_'+decision,'access_request',rid,{'comment':comment});return {'ok':True}
    m=re.fullmatch(r'/api/organizations/(\d+)',path)
    if m and verb=='PATCH':
        app.admin(u);oid=int(m[1]);old=db.execute('SELECT * FROM organizations WHERE id=?',(oid,)).fetchone();require(old,'Организация не найдена',404)
        vals={k:b.get(k,old[k]) for k in ('name','kind','address','active')};vals['name']=text(vals['name']);vals['kind']=text(vals['kind']);vals['address']=str(vals['address'])[:200]
        require(isinstance(vals['active'],bool) or vals['active'] in (0,1),'Некорректная активность')
        db.execute('UPDATE organizations SET name=?,kind=?,address=?,active=? WHERE id=?',(*vals.values(),oid))
        if not vals['active']:db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE org_id=?)',(oid,))
        audit(db,u,'organization_updated','organization',oid,{'before':dict(old),'after':vals});return {'ok':True}
    m=re.fullmatch(r'/api/users/(\d+)',path)
    if m and verb=='PATCH':
        app.admin(u);uid=int(m[1]);old=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();require(old,'Пользователь не найден',404)
        role=b.get('role',old['role']);require(role in ('operator','reviewer','admin','viewer'),'Неверная роль')
        active=b.get('active',old['active']);require(isinstance(active,bool) or active in (0,1),'Неверная активность')
        require(uid!=u['id'] or (role=='admin' and active),'Нельзя отключить или понизить собственный аккаунт администратора',409)
        if old['role']=='admin' and old['active'] and (role!='admin' or not active):require(db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]>1,'Последний администратор должен остаться активным',409)
        oid=b.get('org_id',old['org_id']) if role=='operator' else None
        if role=='operator':require(db.execute('SELECT 1 FROM organizations WHERE id=? AND active=1',(oid,)).fetchone(),'Выберите действующую организацию')
        name=text(b.get('name',old['name']));email=str(b.get('email',old['email']))[:200]
        db.execute('UPDATE users SET name=?,email=?,role=?,org_id=?,active=? WHERE id=?',(name,email,role,oid,active,uid))
        pwd=b.get('password')
        if pwd:require(len(text(pwd,200))>=12,'Пароль — от 12 символов');db.execute('UPDATE users SET password_hash=? WHERE id=?',(S.hash_password(pwd),uid))
        db.execute('DELETE FROM sessions WHERE user_id=?',(uid,));audit(db,u,'user_updated','user',uid,{'name':name,'email':email,'role':role,'org_id':oid,'active':active,'password_reset':bool(pwd)});return {'ok':True}
    if path=='/api/audit' and verb=='GET':
        app.admin(u);return [dict(r) for r in db.execute("SELECT a.*,coalesce(u.name,'Заявитель') actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200")]
    m=re.fullmatch(r'/api/wastes/(\d+)/approve',path)
    if m and verb=='POST':
        app.admin(u);wid=int(m[1]);w=db.execute('SELECT * FROM wastes WHERE id=?',(wid,)).fetchone();require(w,'Вид не найден',404)
        rec=b.get('recyclable',w['recyclable']);require(rec in ('Да','Нет','Частично'),'Неверный признак')
        density=number(b.get('density',w['density']),0.001,20,True) if w['unit']=='л' else None
        ef=number(b.get('ef_landfill',w['ef_landfill']),0,100000,True)
        source=str(b.get('source',w['source'])).strip()[:1000]
        require(ef is None or (len(source)>5 and not source.startswith('Добавлен пользователем')),'Для коэффициента укажите обоснованный источник')
        method=b.get('default_method','TBO');require(method in app.allowed_methods(w),'Недопустимый способ по умолчанию')
        db.execute('UPDATE wastes SET approved=1,recyclable=?,density=?,ef_landfill=?,source=?,default_method=? WHERE id=?',(rec,density,ef,source,method,wid))
        # Existing entries retain historical snapshots. Pending draft entries need explicit re-save to use new factors.
        audit(db,u,'waste_approved','waste',wid,{'recyclable':rec,'density':density,'ef_landfill':ef,'source':source,'default_method':method})
        return {'ok':True,'notice':'Вид утверждён. Коэффициенты существующих записей не переписываются; для нового расчёта исправьте черновые записи.'}
    m=re.fullmatch(r'/api/reports/(\d+)/reminder',path)
    if m and verb=='GET':
        r=app.report(db,u,int(m[1]));day=calendar.monthrange(int(r['month'][:4]),int(r['month'][5:]))[1]
        return {'month':r['month'],'due':r['month']+f'-{day:02d}','note':'Срок по умолчанию — последний день отчётного месяца. Уточните регламент подразделения.'}
    m=re.fullmatch(r'/api/entries/(\d+)/photo',path)
    if m:
        eid=int(m[1]);e=db.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone();require(e,'Запись не найдена',404);r=app.report(db,u,e['report_id'],write=verb in ('POST','DELETE'))
        if verb=='GET':
            photo=db.execute('SELECT * FROM photos WHERE entry_id=?',(eid,)).fetchone();require(photo,'Фото отсутствует',404)
            headers.append(('Content-Type',photo['mime']));return bytes(photo['data'])
        if verb=='DELETE':db.execute('DELETE FROM photos WHERE entry_id=?',(eid,));app.history(db,r['id'],u['id'],'photo_deleted',comment=f'Запись #{eid}');return {'ok':True}
        if verb=='POST':
            encoded=b.get('data');require(isinstance(encoded,str) and len(encoded)<=700000,'Фото слишком большое: максимум 500 КБ')
            try:raw=base64.b64decode(encoded,validate=True)
            except Exception:raise S.ApiError('Некорректное фото')
            mime=b.get('mime');require(0<len(raw)<=500000,'Фото слишком большое: максимум 500 КБ')
            require((mime=='image/jpeg' and raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9')) or (mime=='image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in raw[-16:]),'Разрешены только JPEG/PNG с корректной сигнатурой')
            db.execute('INSERT OR REPLACE INTO photos VALUES(?,?,?,?,?)',(eid,mime,raw,u['id'],S.now()));app.history(db,r['id'],u['id'],'photo_added',comment=f'Запись #{eid}: {len(raw)} байт');return {'ok':True}
    m=re.fullmatch(r'/api/organizations/(\d+)/qr',path)
    if m and verb=='GET':
        oid=int(m[1]);app.org_scope(u,oid);require(db.execute('SELECT 1 FROM organizations WHERE id=?',(oid,)).fetchone(),'Организация не найдена',404)
        base=q.get('base','http://'+env.get('HTTP_HOST','localhost:8000')).rstrip('/');url=urlsplit(base)
        require(url.scheme in ('http','https') and url.hostname and not url.username and not url.password and not url.query and not url.fragment and url.path in ('','/'),'Укажите только адрес сервера http://IPv4:порт')
        require(len(base)<200,'Слишком длинный адрес')
        vendor=Path(__file__).parent/'vendor';sys.path.insert(0,str(vendor)) if str(vendor) not in sys.path else None
        import qrcode
        qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=1,border=4);qr.add_data(base+'/?'+urlencode({'org':oid}));qr.make(fit=True);matrix=qr.get_matrix();n=len(matrix)
        shapes=''.join(f'M{x} {y}h1v1h-1z' for y,row in enumerate(matrix) for x,v in enumerate(row) if v)
        payload=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="white"/><path d="{shapes}" fill="black"/></svg>'.encode()
        headers.append(('Content-Type','image/svg+xml'));return payload
    if path=='/api/comparison' and verb=='GET':
        try:start=date.fromisoformat(q.get('from',''));end=date.fromisoformat(q.get('to',''))
        except ValueError:raise S.ApiError('Укажите начало и конец периода')
        require(start<=end<=S.today(),'Некорректный диапазон')
        def shift(d,months):
            m=S.month_shift(d.strftime('%Y-%m'),months);return date.fromisoformat(m+f'-{min(d.day,calendar.monthrange(int(m[:4]),int(m[5:]))[1]):02d}')
        full_end=end.day==calendar.monthrange(end.year,end.month)[1]
        months=(end.year-start.year)*12+end.month-start.month+1
        if start.day==1 and full_end:
            before_end=start-timedelta(days=1);before_start=shift(start,-months)
        else:
            days=(end-start).days+1;before_end=start-timedelta(days=1);before_start=before_end-timedelta(days=days-1)
        qa={k:v for k,v in q.items() if k not in ('from','to')}
        current=app.analytics(db,u,dict(qa,**{'from':start.isoformat(),'to':end.isoformat()}));previous=app.analytics(db,u,dict(qa,**{'from':before_start.isoformat(),'to':before_end.isoformat()}))
        prior=previous['mass_kg'];delta=current['mass_kg']-prior
        return {'current_from':str(start),'current_to':str(end),'previous_from':str(before_start),'previous_to':str(before_end),'current_kg':current['mass_kg'],'previous_kg':prior,'delta_kg':round(delta,3),'change_percent':round(delta/prior*100,1) if prior else None,'note':'Полные месяцы/кварталы/годы сравниваются с предыдущим календарным интервалом. Иной диапазон — с предыдущим диапазоном той же длины. Это сравнение учтённой массы, не доказательство сокращения потерь.'}
    return UNHANDLED


def validate_snapshots(app,db,rid):
    S=importlib.import_module(app.__class__.__module__)
    def equal(a,b):return a is None and b is None or a is not None and b is not None and abs(a-b)<0.00001
    for e in db.execute('SELECT * FROM entries WHERE report_id=?',(rid,)):
        w=db.execute('SELECT * FROM wastes WHERE id=?',(e['waste_id'],)).fetchone()
        expected=e['quantity'] if w['unit']=='кг' else e['quantity']*w['density'] if w['density'] else None
        S.require(equal(e['mass_kg'],expected) and equal(e['ef_base'],w['ef_landfill']) and equal(e['ef_actual'],app.factor(w,e['method'])),'Параметры вида изменились после взвешивания. Верните отчёт с замечанием: сотрудник должен пересохранить соответствующие записи в черновике.',409)

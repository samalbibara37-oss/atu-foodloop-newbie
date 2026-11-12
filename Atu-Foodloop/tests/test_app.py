"""Integration tests for the real WSGI application and SQLite database."""
import io,json,sqlite3,sys,tempfile,unittest,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application,DEMO_PASSWORD,today,month_shift,xlsx_bytes

class Client:
 def __init__(self,app):self.app=app;self.cookie='';self.csrf=''
 def call(self,path,method='GET',data=None,csrf=True,origin=None):
  p,_,q=path.partition('?');payload=json.dumps(data or {}).encode()
  env={'REQUEST_METHOD':method,'PATH_INFO':p,'QUERY_STRING':q,'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(payload)),'wsgi.input':io.BytesIO(payload),'HTTP_COOKIE':self.cookie,'HTTP_HOST':'localhost:8000','REMOTE_ADDR':'127.0.0.1'}
  if csrf:env['HTTP_X_CSRF_TOKEN']=self.csrf
  if origin:env['HTTP_ORIGIN']=origin
  response={}
  def start(status,headers):response.update(status=int(status[:3]),headers=dict(headers))
  body=b''.join(self.app(env,start))
  if 'Set-Cookie' in response['headers']:self.cookie=response['headers']['Set-Cookie'].split(';')[0]
  try:result=json.loads(body)
  except (ValueError,UnicodeDecodeError):result=body
  return response['status'],result,response['headers']
 def login(self,login):
  status,res,_=self.call('/api/login','POST',{'login':login,'password':DEMO_PASSWORD});assert status==200,res
  self.csrf=res['csrf'];return self

class AppTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.seed=Path(cls.temp.name)/'seed.db';Application(cls.seed,demo=True)
 @classmethod
 def tearDownClass(cls):cls.temp.cleanup()
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
  with sqlite3.connect(self.seed) as src,sqlite3.connect(self.path) as dst:src.backup(dst)
  self.app=Application(self.path);self.op=Client(self.app).login('canteen');self.rev=Client(self.app).login('reviewer');self.adm=Client(self.app).login('admin');self.month=today().strftime('%Y-%m')
  self.boot=self.op.call('/api/bootstrap')[1];self.waste=next(w for w in self.boot['wastes'] if w['name']=='Очистки овощей')
 def tearDown(self):self.tmp.cleanup()
 def report(self):return self.op.call('/api/reports','POST',{'month':self.month})[1]['id']
 def entry(self,rid,**kw):
  body={'waste_id':self.waste['id'],'quantity':10,'date':today().isoformat(),'method':'COMPOST'};body.update(kw)
  return self.op.call(f'/api/reports/{rid}/entries','POST',body)
 def transition(self,c,rid,status,comment=''):return c.call(f'/api/reports/{rid}/transition','POST',{'status':status,'comment':comment})
 def accept(self,rid):
  for c,s in [(self.op,'submitted'),(self.rev,'reviewing'),(self.rev,'accepted')]:self.assertEqual(self.transition(c,rid,s)[0],200)
 def test_full_return_correct_accept_export(self):
  rid=self.report();code,e,_=self.entry(rid);self.assertEqual(code,200);self.assertEqual(len(self.op.call('/api/registry?from='+self.month+'-01')[1]),0)
  self.assertEqual(self.transition(self.op,rid,'submitted')[0],200);self.assertEqual(self.entry(rid,quantity=20)[0],409)
  self.assertEqual(self.transition(self.rev,rid,'reviewing')[0],200);self.assertEqual(self.transition(self.rev,rid,'returned')[0],400)
  self.assertEqual(self.transition(self.rev,rid,'returned','Исправить вес')[0],200)
  self.assertEqual(self.entry(rid)[0],409)
  self.assertEqual(self.transition(self.op,rid,'draft')[0],200)
  patch={'waste_id':self.waste['id'],'quantity':12,'date':today().isoformat(),'method':'COMPOST'}
  self.assertEqual(self.op.call('/api/entries/'+str(e['id']),'PATCH',patch)[0],200);self.accept(rid)
  self.assertEqual(self.entry(rid)[0],409);self.assertEqual(self.op.call('/api/entries/'+str(e['id']),'DELETE',{})[0],409)
  rows=self.rev.call('/api/registry?from='+self.month+'-01')[1];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['quantity'],12)
  a=self.rev.call('/api/analytics?from='+self.month+'-01')[1];self.assertAlmostEqual(a['co2_saving_kg'],12/1000*(700.21-8.88),places=3)
  history=self.op.call('/api/reports/'+str(rid))[1]['history'];self.assertEqual([h['to_status'] for h in history if h['to_status']],['draft','submitted','reviewing','returned','draft','submitted','reviewing','accepted'])
  code,x,headers=self.rev.call('/api/export?from='+self.month+'-01');self.assertEqual(code,200);self.assertIn('spreadsheetml',headers['Content-Type'])
  with zipfile.ZipFile(io.BytesIO(x)) as z:
   for name in z.namelist():ET.fromstring(z.read(name))
   self.assertIn('Очистки овощей',z.read('xl/worksheets/sheet1.xml').decode())
 def test_cross_organization_access(self):
  rid=self.report();c=Client(self.app).login('coffee');self.assertEqual(c.call('/api/reports/'+str(rid))[0],403)
  self.assertEqual(c.call(f'/api/reports/{rid}/entries','POST',{})[0],403);self.assertEqual(c.call('/api/registry?org_id=5')[1],[]);self.assertEqual(self.entry(rid,waste_id=1)[0],400)
 def test_rbac_and_csrf(self):
  rid=self.report();self.entry(rid);self.assertEqual(self.transition(self.op,rid,'accepted')[0],403)
  self.assertEqual(self.op.call('/api/users')[0],403);self.assertEqual(self.op.call('/api/export')[0],403)
  self.assertEqual(self.op.call('/api/reports','POST',{'month':self.month},csrf=False)[0],403)
  self.assertEqual(self.op.call('/api/reports','POST',{'month':self.month},origin='https://evil.example')[0],403);self.assertEqual(Client(self.app).call('/api/registry')[0],401)
 def test_duplicates_and_idempotency(self):
  rid=self.report();a=self.entry(rid,request_id='same')[1];b=self.entry(rid,request_id='same')[1];self.assertEqual(a['id'],b['id']);self.assertTrue(b['replayed'])
  code,data,_=self.entry(rid);self.assertEqual(code,409);self.assertTrue(data['duplicate']);self.assertEqual(self.entry(rid,confirm_duplicate=True)[0],200);self.assertEqual(len(self.op.call('/api/reports/'+str(rid))[1]['entries']),2)
 def test_idempotency_conflicting_content(self):
  rid=self.report();self.assertEqual(self.entry(rid,request_id='one')[0],200)
  self.assertEqual(self.entry(rid,request_id='one',quantity=11)[0],409)
 def test_named_filter_across_organizations(self):
  from urllib.parse import urlencode
  name='Картонная тара';query=urlencode({'waste_name':name})
  rows=self.rev.call('/api/registry?'+query)[1]
  self.assertTrue(rows);self.assertEqual({r['waste_name'] for r in rows},{name});self.assertGreater(len({r['org_id'] for r in rows}),1)
  status,body,_=self.rev.call('/api/export?'+query);self.assertEqual(status,200)
  with zipfile.ZipFile(io.BytesIO(body)) as z:
   xml=z.read('xl/worksheets/sheet2.xml').decode();self.assertIn(name,xml);self.assertNotIn('Очистки овощей',xml)
 def test_invalid_quantity_and_dates(self):
  rid=self.report()
  for v in [0,-1,10001,'hello',float('nan'),float('inf')]:self.assertEqual(self.entry(rid,quantity=v)[0],400)
  self.assertEqual(self.entry(rid,date='2099-01-01')[0],400);self.assertEqual(self.entry(rid,date=month_shift(self.month,-1)+'-01')[0],400);self.assertEqual(self.op.call('/api/reports','POST',{'month':'2026-13'})[0],400)
 def test_oil_mass_and_co2(self):
  rid=self.report();oil=next(w for w in self.boot['wastes'] if w['unit']=='л');self.assertEqual(self.entry(rid,waste_id=oil['id'],quantity=10,method='OIL')[0],200);self.accept(rid)
  a=self.op.call('/api/analytics?from='+self.month+'-01')[1];self.assertEqual(a['mass_kg'],9.2);self.assertAlmostEqual(a['co2_saving_kg'],9.2/1000*(700.21-6.41),places=3)
 def test_new_waste_unknown_density_deactivation(self):
  code,w,_=self.op.call('/api/wastes','POST',{'name':'Новый жидкий остаток','unit':'л','category':'Органические','recyclable':'Нет'});self.assertEqual(code,200)
  self.assertEqual(self.adm.call('/api/wastes/'+str(w['id'])+'/approve','POST',{})[0],200)
  rid=self.report();self.assertEqual(self.entry(rid,waste_id=w['id'],method='TBO')[0],200);self.accept(rid)
  a=self.op.call('/api/analytics?from='+self.month+'-01')[1];self.assertEqual(a['unknown_mass_records'],1);self.assertEqual(a['unknown_co2_records'],1)
  self.assertEqual(self.adm.call('/api/wastes/'+str(w['id']),'PATCH',{'active':False})[0],200);self.assertEqual(len(self.op.call('/api/registry?from='+self.month+'-01')[1]),1)
 def test_donation_is_not_waste_or_co2(self):
  rid=self.report();w=next(w for w in self.boot['wastes'] if 'нереализованной' in w['name']);self.assertEqual(self.entry(rid,waste_id=w['id'],method='DONATE')[0],400)
  self.assertEqual(self.entry(rid,waste_id=w['id'],method='DONATE',safe_confirm=True)[0],200);self.accept(rid)
  a=self.op.call('/api/analytics?from='+self.month+'-01')[1];self.assertEqual(a['mass_kg'],0);self.assertEqual(a['donated_kg'],10);self.assertEqual(a['co2_saving_kg'],0)
 def test_method_compatibility(self):
  rid=self.report();w=next(w for w in self.boot['wastes'] if 'мясопродуктов' in w['name']);self.assertEqual(self.entry(rid,waste_id=w['id'],method='COMPOST')[0],400)
  self.assertEqual(self.entry(rid,waste_id=w['id'],method='AD')[0],200);self.assertEqual(self.entry(rid,method='RECYCLE')[0],400)
 def test_seed_and_missing(self):
  self.assertEqual(len(self.adm.call('/api/bootstrap')[1]['organizations']),5);reports=self.rev.call('/api/reports')[1]
  for oid in range(1,6):self.assertGreaterEqual(len({r['month'] for r in reports if r['org_id']==oid}),3)
  self.assertEqual(len(self.rev.call('/api/missing?month='+self.month)[1]),5);rid=self.report();self.entry(rid);self.accept(rid);self.assertEqual(len(self.rev.call('/api/missing?month='+self.month)[1]),4)
 def test_admin_adds_org_and_user(self):
  oid=self.adm.call('/api/organizations','POST',{'name':'Новый буфет','kind':'Кофейня'})[1]['id']
  data={'login':'new_user','name':'Новый сотрудник','role':'operator','org_id':oid,'password':DEMO_PASSWORD}
  self.assertEqual(self.adm.call('/api/users','POST',data)[0],200);c=Client(self.app).login('new_user');self.assertEqual(len(c.call('/api/bootstrap')[1]['organizations']),1);self.assertEqual(c.call('/api/registry')[1],[])
  self.assertEqual(self.adm.call('/api/users','POST',dict(data,login='bad_user',org_id=999))[0],400)
 def test_actions_and_readonly(self):
  c=Client(self.app).login('director');self.assertEqual(c.call('/api/actions','POST',{'title':'x','org_id':5})[0],403)
  code,a,_=self.op.call('/api/actions','POST',{'title':'Готовить партиями','due':today().isoformat()});self.assertEqual(code,200)
  c=Client(self.app).login('coffee');self.assertEqual(c.call('/api/actions/'+str(a['id']),'PATCH',{'result':'done'})[0],403);self.assertEqual(self.op.call('/api/actions/'+str(a['id']),'PATCH',{'result':'Нужен дальнейший пилот'})[0],200)
 def test_password_logout_rate_limit(self):
  self.assertEqual(self.op.call('/api/password','POST',{'old':'bad','new':'SecurePassword123'})[0],403);self.assertEqual(self.op.call('/api/password','POST',{'old':DEMO_PASSWORD,'new':'SecurePassword123'})[0],200)
  self.assertEqual(self.op.call('/api/logout','POST',{})[0],200);self.assertEqual(self.op.call('/api/me')[0],401);c=Client(self.app)
  for _ in range(10):self.assertEqual(c.call('/api/login','POST',{'login':'fake','password':'bad'})[0],401)
  self.assertEqual(c.call('/api/login','POST',{'login':'fake','password':'bad'})[0],429)
 def test_export_injection_and_static_path(self):
  data=xlsx_bytes([('Report',[['Name'],['=HYPERLINK("evil")']])])
  with zipfile.ZipFile(io.BytesIO(data)) as z:self.assertNotIn('<f>',z.read('xl/worksheets/sheet1.xml').decode())
  self.assertEqual(self.op.call('/static/../server.py')[0],404);self.assertEqual(self.op.call('/server.py')[0],404)
 def test_db_lock_persistence(self):
  rid=self.report();self.entry(rid);self.accept(rid)
  with self.app.connect() as db:
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM entries WHERE report_id=?',(rid,))
  c=Client(Application(self.path)).login('reviewer');self.assertEqual(c.call('/api/reports/'+str(rid))[1]['status'],'accepted')
 def test_clean_production_database(self):
  app=Application(Path(self.tmp.name)/'prod.db',admin_password='OnlyAdminPassword123');c=Client(app)
  self.assertEqual(c.call('/api/login','POST',{'login':'admin','password':DEMO_PASSWORD})[0],401)
  code,res,_=c.call('/api/login','POST',{'login':'admin','password':'OnlyAdminPassword123'});self.assertEqual(code,200);c.csrf=res['csrf'];self.assertFalse(c.call('/api/me')[1]['demo']);self.assertEqual(len(c.call('/api/bootstrap')[1]['organizations']),5)
 def test_access_request_requires_admin_approval(self):
  c=Client(self.app);body={'login':'candidate','name':'Сотрудник','email':'staff@example.org','org_id':5,'password':DEMO_PASSWORD,'role':'admin','active':True}
  self.assertEqual(c.call('/api/access-requests','POST',body)[0],200)
  self.assertEqual(c.call('/api/login','POST',{'login':'candidate','password':DEMO_PASSWORD})[0],401)
  rows=self.adm.call('/api/access-requests')[1];rid=rows[0]['id']
  self.assertEqual(self.rev.call('/api/access-requests/'+str(rid),'PATCH',{'status':'approved'})[0],403)
  self.assertEqual(self.adm.call('/api/access-requests/'+str(rid),'PATCH',{'status':'approved'})[0],200)
  c.login('candidate');self.assertEqual(c.call('/api/me')[1]['role'],'operator');self.assertEqual(c.call('/api/me')[1]['org_id'],5)
  self.assertEqual(c.call('/api/users')[0],403)
 def test_reject_request_comment_and_no_account(self):
  body={'login':'denied_user','name':'Сотрудник','email':'staff@example.org','org_id':5,'password':DEMO_PASSWORD}
  Client(self.app).call('/api/access-requests','POST',body);rid=self.adm.call('/api/access-requests')[1][0]['id']
  self.assertEqual(self.adm.call('/api/access-requests/'+str(rid),'PATCH',{'status':'rejected'})[0],400)
  self.assertEqual(self.adm.call('/api/access-requests/'+str(rid),'PATCH',{'status':'rejected','comment':'Не подтверждена принадлежность'})[0],200)
  self.assertEqual(Client(self.app).call('/api/login','POST',{'login':'denied_user','password':DEMO_PASSWORD})[0],401)
 def test_block_user_revokes_existing_session(self):
  uid=self.op.call('/api/me')[1]['id'];self.assertEqual(self.adm.call('/api/users/'+str(uid),'PATCH',{'active':False})[0],200)
  self.assertEqual(self.op.call('/api/me')[0],401);self.assertEqual(Client(self.app).call('/api/login','POST',{'login':'canteen','password':DEMO_PASSWORD})[0],401)
  self.assertEqual(self.adm.call('/api/users/1','PATCH',{'active':False})[0],409)
 def test_organization_disable_and_audit(self):
  self.assertEqual(self.op.call('/api/organizations/5','PATCH',{'active':False})[0],403)
  self.assertEqual(self.adm.call('/api/organizations/5','PATCH',{'active':False})[0],200)
  self.assertEqual(self.op.call('/api/me')[0],401)
  self.assertEqual(Client(self.app).call('/api/login','POST',{'login':'canteen','password':DEMO_PASSWORD})[0],403)
  logs=self.adm.call('/api/audit')[1];self.assertEqual(logs[0]['action'],'organization_updated')
 def test_role_change_revokes_and_restricts(self):
  uid=self.op.call('/api/me')[1]['id'];self.assertEqual(self.adm.call('/api/users/'+str(uid),'PATCH',{'org_id':4})[0],200)
  self.assertEqual(self.op.call('/api/me')[0],401);c=Client(self.app).login('canteen');self.assertEqual(c.call('/api/me')[1]['org_id'],4)
 def test_pending_catalog_prevents_acceptance(self):
  w=self.op.call('/api/wastes','POST',{'name':'Чайная заварка','unit':'кг','category':'Органические','recyclable':'Да'})[1]
  rid=self.report();self.entry(rid,waste_id=w['id']);self.transition(self.op,rid,'submitted');self.transition(self.rev,rid,'reviewing')
  self.assertEqual(self.transition(self.rev,rid,'accepted')[0],409)
  self.assertEqual(self.rev.call('/api/wastes/'+str(w['id'])+'/approve','POST',{})[0],403)
  self.assertEqual(self.adm.call('/api/wastes/'+str(w['id'])+'/approve','POST',{'ef_landfill':1})[0],400)
  self.assertEqual(self.adm.call('/api/wastes/'+str(w['id'])+'/approve','POST',{'recyclable':'Да'})[0],200)
  self.assertEqual(self.transition(self.rev,rid,'accepted')[0],200)
 def test_period_comparison_quarter_and_filters(self):
  status,c,_=self.rev.call('/api/comparison?from=2026-07-01&to=2026-09-30');self.assertEqual(status,200);self.assertEqual(c['previous_from'],'2026-04-01');self.assertEqual(c['previous_to'],'2026-06-30')
  self.assertEqual(c['change_percent'],None)
  a=self.rev.call('/api/analytics?from=2026-07-01&to=2026-09-30')[1];self.assertEqual(c['current_kg'],a['mass_kg'])
  self.assertEqual(self.rev.call('/api/comparison?from=2026-09-30&to=2026-07-01')[0],400)
 def test_photo_scope_and_accepted_lock(self):
  import base64
  rid=self.report();eid=self.entry(rid)[1]['id'];path='/api/entries/'+str(eid)+'/photo'
  # Minimal valid 1px PNG, format and authorization are tested through the actual API.
  raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwL8AAAAASUVORK5CYII=')
  body={'mime':'image/png','data':base64.b64encode(raw).decode()}
  self.assertEqual(self.op.call(path,'POST',body)[0],200);self.assertEqual(self.op.call(path)[1],raw)
  self.assertEqual(Client(self.app).login('coffee').call(path)[0],403)
  self.assertEqual(self.op.call(path,'POST',{'mime':'image/svg+xml','data':base64.b64encode(b'<svg/>').decode()})[0],400)
  self.accept(rid);self.assertEqual(self.op.call(path,'POST',body)[0],409);self.assertEqual(self.op.call(path,'DELETE',{})[0],409)
 def test_qr_is_scoped_and_contains_svg(self):
  code,data,h=self.op.call('/api/organizations/5/qr?base=http%3A%2F%2F192.168.1.3%3A8000');self.assertEqual(code,200);self.assertIn('image/svg+xml',h['Content-Type']);self.assertIn(b'<svg',data)
  self.assertEqual(self.op.call('/api/organizations/4/qr')[0],403)
  self.assertEqual(self.op.call('/api/organizations/5/qr?base=javascript%3Aevil')[0],400)
 def test_private_has_templates_without_shared_accounts(self):
  app=Application(Path(self.tmp.name)/'private.db',admin_password='PrivatePassword123');c=Client(app)
  code,res,_=c.call('/api/login','POST',{'login':'admin','password':'PrivatePassword123'});self.assertEqual(code,200);c.csrf=res['csrf']
  b=c.call('/api/bootstrap')[1];self.assertEqual(len(b['organizations']),5);self.assertGreater(len(b['wastes']),20)
  self.assertEqual(c.call('/api/registry')[1],[]);self.assertEqual(len(c.call('/api/users')[1]),1)
  self.assertEqual(Client(app).call('/api/login','POST',{'login':'reviewer','password':DEMO_PASSWORD})[0],401)
 def test_export_includes_familiar_ledgers(self):
  status,data,_=self.rev.call('/api/export');self.assertEqual(status,200)
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   self.assertIn('Организация 1',z.read('xl/workbook.xml').decode());self.assertIn('тара + кости',z.read('xl/worksheets/sheet4.xml').decode())

 def test_changed_density_requires_resaving_before_accept(self):
  w=self.op.call('/api/wastes','POST',{'name':'Жидкий остаток','unit':'л','category':'Органические','recyclable':'Нет','density':2})[1]
  rid=self.report();eid=self.entry(rid,waste_id=w['id'],quantity=10,method='TBO')[1]['id']
  self.adm.call('/api/wastes/'+str(w['id'])+'/approve','POST',{'density':1})
  self.transition(self.op,rid,'submitted');self.transition(self.rev,rid,'reviewing');self.assertEqual(self.transition(self.rev,rid,'accepted')[0],409)
  self.transition(self.rev,rid,'returned','Пересохраните массу после уточнения плотности');self.transition(self.op,rid,'draft')
  self.assertEqual(self.op.call('/api/entries/'+str(eid),'PATCH',{'waste_id':w['id'],'quantity':10,'date':today().isoformat(),'method':'TBO'})[0],200)
  self.accept(rid);self.assertEqual(self.rev.call('/api/registry?from='+self.month+'-01')[1][0]['mass_kg'],10)
if __name__=='__main__':unittest.main(verbosity=2)

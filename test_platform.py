import io, json, os, time
from pathlib import Path
import tempfile
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='inai-test-')
os.environ['APP_TOKEN']='test-only-token-not-for-production-123456'
import pytest, fitz
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient
import app, engine, model

@pytest.fixture(scope='module')
def client():
    with TestClient(app.app) as c: yield c

def headers(): return {'Authorization':'Bearer '+os.environ['APP_TOKEN']}

def pdf_bytes():
    doc=fitz.open()
    for text in ['Scholarship applications close on 15 October 2026. Students must submit a transcript.', 'The interview takes place in Chennai. Selected students receive a laptop and training.']:
        doc.new_page().insert_text((50,80),text,fontsize=10)
    result=doc.tobytes();doc.close();return result

def wait(c,id):
    for _ in range(100):
        row=c.get('/api/documents/'+id,headers=headers()).json()
        if row['status'] not in ('processing','queued'):return row
        time.sleep(.05)
    pytest.fail('Extraction did not finish')

def test_auth_origin_and_upload_validation(client):
    assert client.get('/api/documents').status_code==401
    assert client.get('/api/documents',headers={'Authorization':'Bearer wrong'}).status_code==401
    assert client.post('/api/documents',headers={**headers(),'Origin':'https://attacker.invalid'}).status_code==403
    assert client.post('/api/documents',headers=headers(),files={'file':('x.exe',b'text')}).status_code==415
    assert client.post('/api/documents',headers=headers(),files={'file':('empty.pdf',b'')}).status_code==400
    assert client.post('/api/documents',headers=headers(),data={'language':'xxx'},files={'file':('x.pdf',pdf_bytes())}).status_code==422

def test_complete_document_lifecycle(client):
    r=client.post('/api/documents',headers=headers(),files={'file':('../../scholarship.pdf',pdf_bytes())},data={'language':'eng'})
    assert r.status_code==202,r.text
    id=r.json()['id'];row=wait(client,id)
    assert row['status']=='ready',row
    assert row['name']=='scholarship.pdf'
    assert len(row['pages'])==2 and row['pages'][1]['method']=='embedded'
    r=client.post(f'/api/documents/{id}/search',headers=headers(),json={'query':'Chennai interview'}).json()
    assert r['results'][0]['page']==2
    r=client.get(f'/api/documents/{id}/summary',headers=headers()).json()
    assert all(x['text'] in row['pages'][x['page']-1]['text'] for x in r['sentences'])
    r=client.get(f'/api/documents/{id}/export?format=json',headers=headers())
    assert r.json()==row['pages']
    assert client.post(f'/api/documents/{id}/ai',headers=headers(),json={'action':'translate','page':99}).status_code==422
    assert client.post(f'/api/documents/{id}/ai',headers=headers(),json={'action':'translate','page':1}).status_code==503
    assert client.post(f'/api/documents/{id}/search',headers=headers(),json={'query':'unicorn'}).json()['results']==[]
    client.patch('/api/documents/'+id,headers=headers(),json={'archived':True})
    assert not any(x['id']==id for x in client.get('/api/documents',headers=headers()).json())
    assert any(x['id']==id for x in client.get('/api/documents?archived=true',headers=headers()).json())
    client.patch('/api/documents/'+id,headers=headers(),json={'archived':False})
    # Open a fresh database connection to verify persisted data, independent of HTTP state.
    with app.db() as con: assert json.loads(con.execute('SELECT pages FROM documents WHERE id=?',(id,)).fetchone()[0])==row['pages']

def test_invalid_pdf_fails_cleanly(client):
    r=client.post('/api/documents',headers=headers(),files={'file':('broken.pdf',b'%PDF-broken')})
    row=wait(client,r.json()['id']);assert row['status']=='failed' and row['error']

def test_tamil_unicode_retrieval_and_abstention():
    pages=[{'number':1,'text':'தமிழ் மொழி ஆவணங்கள் தேடல் மற்றும் கல்வி உதவித்தொகை.'},{'number':2,'text':'English software engineering documents.'}]
    assert engine.retrieve(pages,'உதவித்தொகை')[0]['page']==1
    assert engine.retrieve(pages,'missingword')==[]
    assert 'மொழி' in engine.tokens('தமிழ் மொழி')

def test_real_english_image_ocr(tmp_path):
    im=Image.new('RGB',(1600,300),'white')
    draw=ImageDraw.Draw(im)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',42)
    draw.text((30,90),'Scholarship deadline 15 October 2026',font=font,fill='black')
    path=tmp_path/'scan.png';im.save(path)
    pages=engine.extract(path,'eng')
    assert 'October' in pages[0]['text'] and pages[0]['method']=='ocr'

def test_model_contract_and_invalid_citations(monkeypatch):
    monkeypatch.setattr(model,'MODEL','contract-test-model')
    class Reply:
        def raise_for_status(self):pass
        def json(self):return {'message':{'content':json.dumps({'text':'Answer [p. 2]','pages':[2]})}}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,json):
            assert 'untrusted' in json['messages'][0]['content']
            return Reply()
    monkeypatch.setattr(model.httpx,'Client',Client)
    assert model.generate('question',[{'page':2,'text':'Evidence'}],'English')['pages']==[2]
    with pytest.raises(ValueError,match='invalid page'):
        model.generate('question',[{'page':1,'text':'Evidence'}],'Tamil')

def test_real_tamil_image_ocr():
    import pytesseract
    if 'tam' not in pytesseract.get_languages(config=''):
        pytest.skip('Install tesseract-ocr-tam to run Tamil OCR integration test')
    pages=engine.extract(Path(__file__).with_name('sample-tamil.png'),'tam+eng')
    assert 'தமிழ்' in pages[0]['text'] and 'கல்வி' in pages[0]['text']

import base64
import csv
import hashlib
from io import BytesIO, StringIO
import re
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import UraDaily, User, PortalGroup
from app.routes import contract
from app.security import create_session_token, generate_csrf_token, settings
from app.timezone import format_local_datetime
from tests.test_ura_import import sample_csv


@pytest.fixture
def contract_client():
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        admin = User(username='contract_admin',is_admin=True,is_active=True)
        viewer = User(username='contract_viewer',is_admin=False,is_active=True,portal_groups=[PortalGroup(name='portal_view')])
        db.add_all([admin,viewer]); db.commit()
        tokens = [create_session_token(admin.id),create_session_token(viewer.id)]
    application = FastAPI()
    application.state.settings = settings
    application.state.templates = Jinja2Templates(directory='templates')
    application.state.templates.env.filters['local_datetime'] = format_local_datetime
    application.state.templates.env.globals['menu_dashboards_for_user'] = lambda _: []
    application.mount('/static',StaticFiles(directory='static'),name='static')
    application.include_router(contract.router)
    def session():
        with factory() as db:
            yield db
    application.dependency_overrides[get_db] = session
    @application.middleware('http')
    async def csrf_context(request,call_next):
        request.state.csrf_token = generate_csrf_token(request.cookies.get(settings.cookie_name,''))
        return await call_next(request)
    with TestClient(application,follow_redirects=False) as client:
        yield client, tokens, factory
    engine.dispose()


def login(client,token):
    client.cookies.set(settings.cookie_name,token)


def upload(client,token,raw=None):
    return client.post('/admin/contract/ura', data={'csrf_token':generate_csrf_token(token),
        'competencia':'2026-10','projeto':'DETRAN-GO','source_name':'=unsafe.csv',
        'csv_base64':base64.b64encode(raw or sample_csv()).decode()})


def export_params(page):
    return re.search(r'href="(/admin/contract/export[^\"]+formato=json)"',page.text).group(1).replace('&amp;','&')


def test_admin_page_values_details_and_all_exports_match(contract_client):
    client,(admin,_),factory = contract_client
    login(client,admin)
    raw = b'\xef\xbb\xbf'+sample_csv()  # prova BOM + CRLF preservados pelo transporte base64
    assert upload(client,admin,raw).status_code == 303
    page = client.get('/admin/contract?competencia=2026-10&projeto=DETRAN-GO&pontos_base=50')
    assert page.status_code == 200, page.text
    url = export_params(page)
    response = client.get(url)
    assert response.status_code == 200
    evidence = response.json()
    assert evidence['series']['files'][0]['sha256'] == hashlib.sha256(raw).hexdigest()
    assert evidence['simulation']['weighted_points'] == '25.0'
    assert evidence['comparison']['state'] == 'sem_base_contratual'
    for metric in evidence['metrics']:
        assert f'data-evidence-value="{metric["value"]}"' in page.text
    csv_response = client.get(url.replace('formato=json','formato=csv'))
    rows = dict(csv.reader(StringIO(csv_response.content.decode('utf-8-sig')),delimiter=';'))
    assert rows['metrics[0].value'] == evidence['metrics'][0]['value']
    assert rows['series.files[0].name'] == "'=unsafe.csv"
    workbook = load_workbook(BytesIO(client.get(url.replace('formato=json','formato=xlsx')).content))
    data = dict(workbook.active.values)
    assert data['metrics[1].value'] == evidence['metrics'][1]['value']
    formula_cell = next(row[1] for row in workbook.active if row[0].value == 'series.files[0].name')
    assert formula_cell.value == '=unsafe.csv' and formula_cell.data_type == 's'
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(UraDaily)) == 1


def test_new_data_invalidates_export_signature(contract_client):
    client,(admin,_),_ = contract_client
    login(client,admin)
    assert upload(client,admin).status_code == 303
    old_url = export_params(client.get('/admin/contract?competencia=2026-10'))
    assert upload(client,admin,sample_csv(day='23/09/2026')).status_code == 303
    assert client.get(old_url).status_code == 409


def test_auth_csrf_and_invalid_inputs(contract_client):
    client,(admin,viewer),_ = contract_client
    assert client.get('/admin/contract').status_code == 303
    assert client.get('/admin/contract/export').status_code == 303
    login(client,viewer)
    assert client.get('/admin/contract').headers['location'] == '/home'
    assert client.get('/admin/contract/export').headers['location'] == '/home'
    assert upload(client,viewer).headers['location'] == '/home'
    login(client,admin)
    assert client.post('/admin/contract/ura',data={'csv_content':'bad'}).status_code == 403
    for query in ('competencia=2026-13','competencia=2026-09','servico=xyz','meta_base=NaN','direcao=invalid','projeto=invalid%20space'):
        assert client.get('/admin/contract?'+query).status_code == 400
    assert client.get('/admin/contract?competencia=2026-10').status_code == 200

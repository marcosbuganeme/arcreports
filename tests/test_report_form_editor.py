import asyncio
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.models import Report, ReportCategory
from app.routes import reports
from app.security import create_session_token, settings


LONG_SQL = """SELECT
  tickets.id,
  tickets.name,
  users.name AS requester_name,
  categories.name AS category_name
FROM tickets
LEFT JOIN users ON users.id = tickets.users_id_recipient
LEFT JOIN categories ON categories.id = tickets.categories_id
WHERE tickets.is_deleted = 0
ORDER BY tickets.id DESC"""


def post_request_for(user, path):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "app": SimpleNamespace(state=SimpleNamespace()),
            "headers": [
                (b"cookie", f"{settings.cookie_name}={create_session_token(user.id)}".encode())
            ],
        }
    )


def report_form_data(name, sql_query):
    return {
        "name": name,
        "sql_query": sql_query,
        "report_type": "manual",
        "category": "Geral",
        "is_primary": "on",
        "is_active": "on",
    }


def test_report_form_uses_same_codemirror_configuration_as_sql_console():
    template = Path("templates/report_form.html").read_text(encoding="utf-8")

    assert "vendor/codemirror/5.65.16/lib/codemirror.min.css" in template
    assert "vendor/codemirror/5.65.16/lib/codemirror.min.js" in template
    assert "vendor/codemirror/5.65.16/mode/sql/sql.min.js" in template
    assert 'CodeMirror.fromTextArea(sqlQuery' in template
    assert 'mode: "text/x-mysql"' in template
    assert "lineNumbers: true" in template
    assert 'id="sql_query" name="sql_query"' in template
    assert "{{ report.sql_query if report else '' }}" in template
    assert 'sqlEditor.on("change", () => sqlEditor.save())' in template
    assert 'reportForm.addEventListener("submit"' in template


def test_create_and_edit_report_preserve_long_multiline_sql(
    db_session, admin_local_user, monkeypatch
):
    db_session.add(ReportCategory(name="Geral", is_active=True))
    db_session.commit()

    create_data = report_form_data("Relatorio CodeMirror", LONG_SQL)

    async def create_form_data(_request):
        return create_data

    monkeypatch.setattr(reports, "form_data", create_form_data)
    response = asyncio.run(
        reports.create_report(
            post_request_for(admin_local_user, "/reports"), db_session
        )
    )

    assert response.status_code == 303
    created = db_session.query(Report).filter_by(name="Relatorio CodeMirror").one()
    assert created.sql_query == LONG_SQL

    edited_sql = LONG_SQL.replace(
        "WHERE tickets.is_deleted = 0",
        "WHERE tickets.is_deleted = 0\n  AND categories.name IS NOT NULL",
    )
    edit_data = report_form_data("Relatorio CodeMirror editado", edited_sql)

    async def edit_form_data(_request):
        return edit_data

    monkeypatch.setattr(reports, "form_data", edit_form_data)
    response = asyncio.run(
        reports.update_report(
            created.id,
            post_request_for(admin_local_user, f"/reports/{created.id}/edit"),
            db_session,
        )
    )

    assert response.status_code == 303
    db_session.expire_all()
    edited = db_session.get(Report, created.id)
    assert edited.name == "Relatorio CodeMirror editado"
    assert edited.sql_query == edited_sql

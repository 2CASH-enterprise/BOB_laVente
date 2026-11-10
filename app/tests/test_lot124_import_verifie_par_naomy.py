"""
Lot 124 (contrat 1.6, webhook prospectionVerifier ouvert par Naomy le 10/11) : avant d'importer une liste de prospects,
Bob demande à Naomy quelles empreintes exclure (ses clients, ses opposés). Naomy injoignable : rien n'est importé.
"""
import hashlib
import json

import pytest
from sqlalchemy import select

from app.models.prospect import Prospect
from app.services import naomy_bridge as nb
from app.services import prospection_sync as ps
from app.tests.test_lot89_connecteur_naomy import SECRET, naomy_env  # noqa: F401
from app.tests.test_lot119_roles_super_admin import _h, _member
from app.tests.test_lot122_vague_restaurants import _csv, _import


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Naomy:
    def __init__(self, exclude=(), status=200, fail=False):
        self.exclude, self.status, self.fail, self.calls = set(exclude), status, fail, []

    async def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.fail:
            raise OSError("injoignable")
        asked = [e["empreinte"] for e in json.loads(body)["empreintes"]]
        return self.status, {"exclure": [{"empreinte": a, "motif": "client_naomy"} for a in asked if a in self.exclude]}


ROWS = [["Client de Naomy", "", "+2250701000061", "", "", "CI", "Restaurant", "", "1", "non"],
        ["Opposé chez Naomy", "", "", "stop@naomy.ci", "", "CI", "Restaurant", "", "1", "non"],
        ["Libre", "", "+2250701000063", "libre@x.ci", "", "CI", "Restaurant", "", "1", "non"]]


@pytest.mark.asyncio
async def test_naomys_clients_and_oppositions_are_left_out(client, db_session, naomy_env, monkeypatch):
    naomy = Naomy(exclude={h("2250701000061"), h("stop@naomy.ci")})
    monkeypatch.setattr(ps, "http_post_json", naomy)
    _, report = await _import(client, _h(await _member(db_session, "SALES")), _csv(*ROWS))
    assert report["created"] == 1 and report["excluded_naomy"] == 2
    assert [p.company for p in (await db_session.execute(select(Prospect))).scalars().all()] == ["Libre"]
    [(url, body, headers)] = naomy.calls
    assert url == "https://agenc-ai.com/naomy/api/bob/prospection/verifier"
    assert nb.platform_signature_ok(SECRET, headers["X-Platform-Timestamp"], headers["X-Platform-Signature"], body)
    sent = {(e["empreinte"], e["type"]) for e in json.loads(body)["empreintes"]}
    assert sent == {(h("2250701000061"), "telephone"), (h("stop@naomy.ci"), "email"), (h("2250701000063"), "telephone"),
                    (h("libre@x.ci"), "email")}  # empreintes seulement, rien en clair
    assert "libre@x.ci" not in body.decode() and "2250701000063" not in body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("naomy", [Naomy(fail=True), Naomy(status=503), Naomy(status=401)])
async def test_nothing_is_imported_when_naomy_cannot_check(client, db_session, naomy_env, monkeypatch, naomy):
    monkeypatch.setattr(ps, "http_post_json", naomy)
    headers = _h(await _member(db_session, "SALES"))
    files = {"file": ("vague.csv", _csv(*ROWS), "text/csv")}
    preview = (await client.post("/api/v1/superadmin/prospects/import/preview", headers=headers, files=files)).json()
    r = await client.post("/api/v1/superadmin/prospects/import", headers=headers, files=files,
                          data={"mapping": json.dumps(preview["mapping"]), "country": "CI", "sector": "RESTAURANT"})
    assert r.status_code == 422 and "Naomy" in r.json()["detail"] and "rien n'a été importé" in r.json()["detail"]
    assert not (await db_session.execute(select(Prospect))).scalars().all()


@pytest.mark.asyncio
async def test_big_lists_go_by_1000_and_bad_answers_are_refused(naomy_env):
    from app.services.prospection import ProspectError

    naomy = Naomy(exclude={h("p5")})
    items = [(h(f"p{i}"), "email") for i in range(2500)] + [(None, "email"), ("pas-une-empreinte", "email"), (h("x"), "fax")]
    assert await ps.ask_naomy(items, sender=naomy) == {h("p5")}
    assert [len(json.loads(b)["empreintes"]) for _, b, _ in naomy.calls] == [1000, 1000, 500]

    async def weird(url, body, headers):
        return 200, {"autre": []}

    with pytest.raises(ProspectError):
        await ps.ask_naomy([(h("a"), "email")], sender=weird)

    async def foreign(url, body, headers):  # Naomy ne peut pas faire exclure une empreinte qu'on n'a pas demandée
        return 200, {"exclure": [{"empreinte": h("autre"), "motif": "client_naomy"}]}

    assert await ps.ask_naomy([(h("a"), "email")], sender=foreign) == set()


@pytest.mark.asyncio
async def test_nothing_is_asked_while_the_gateway_is_off(client, db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "naomy_platform_secret", "")
    naomy = Naomy()
    monkeypatch.setattr(ps, "http_post_json", naomy)
    _, report = await _import(client, _h(await _member(db_session, "SALES")), _csv(*ROWS))
    assert report["created"] == 3 and naomy.calls == []

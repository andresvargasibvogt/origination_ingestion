"""proyectos-ci portal resolver tests — both real-world patterns + unresolved.

Pattern A (Aragón): regional listing has a per-project link (expediente in the
slug/text) → project page → almacen link.
Pattern B (Valencia): the almacen link is INLINE on the listing, next to the
expediente — no per-project page.
"""

from __future__ import annotations

import httpx
import respx

from annex_ingest.proyectos_ci import (
    _by_expediente,
    _inline_uuids,
    extract_expedientes,
    extract_portal_url,
    extract_province,
    resolve,
)

ALM = "almacen.redsara.es/sending/public/15785610-da5b-4869-b4cd-d9fd1c837dc6"
UUID = "15785610-da5b-4869-b4cd-d9fd1c837dc6"


def test_extract_portal_url() -> None:
    xml = "<p>consultar en https://mptmd.gob.es/portal/delegaciones_gobierno/delegaciones/aragon/proyectos-ci/proyectos y tal</p>"
    assert extract_portal_url(xml).endswith("/aragon/proyectos-ci/proyectos")
    assert extract_portal_url("<p>no portal here</p>") is None


def test_extract_expedientes_filters_noise() -> None:
    exps = extract_expedientes("proyecto FV-168-ALM Herrera", "Ley 39/2015 inversor SUN2000 NIF B-2026123")
    assert "FV-168-ALM" in exps
    assert "B-2026123"[:5] not in {e[:5] for e in exps} or all(not e.startswith("B-20") for e in exps)


def test_extract_expedientes_compound_after_expte() -> None:
    # "(expediente PFot-ALM-180)" → capture the full compound code.
    exps = extract_expedientes("Albacete", "examinado el proyecto (expediente PFot-ALM-180) en esta Dependencia")
    assert "PFOT-ALM-180" in exps


def test_extract_province() -> None:
    assert extract_province("Subdelegación del Gobierno en Albacete, por el que…", "") == {"ALBACETE"}
    assert "NAVARRA" in extract_province("Delegación del Gobierno en Navarra por el que…", "")


def test_by_expediente_matches_in_anchor() -> None:
    anchors = [("https://x/otra", "Otro proyecto"),
               ("https://x/250_PEol_FV_168_alm", "PEol-FV-168-ALM Herrera de los Navarros 35 MW")]
    m = _by_expediente(anchors, {"FV-168-ALM"})
    assert m and m[0].endswith("250_PEol_FV_168_alm")


def test_inline_uuids_proximity() -> None:
    page = f'... Expte. PFot-761 (2022/10). OBJETO: info publica. <a href="https://{ALM}">descargar</a> ...'
    assert _inline_uuids(page, {"PFOT-761"}) == (UUID,)
    # no expediente match → empty
    assert _inline_uuids(page, {"ZZ-999"}) == ()


@respx.mock
async def test_resolve_pattern_a_project_page() -> None:
    portal = "https://mptmd.gob.es/portal/delegaciones_gobierno/delegaciones/aragon/proyectos-ci/proyectos"
    proj = portal + "/250_PEol_FV_168_alm_herrera"
    listing = f'<a href="{proj}">PEol-FV-168-ALM MODULO DE ALMACENAMIENTO HERRERA DE LOS NAVARROS 35 MW</a>'
    project_page = f'cita previa. Documentación: <a href="https://{ALM}">ZIP</a>'
    respx.get(portal).mock(return_value=httpx.Response(200, text=listing))
    respx.get(proj).mock(return_value=httpx.Response(200, text=project_page))
    async with httpx.AsyncClient() as c:
        res = await resolve(c, portal, {"FV-168-ALM"}, set(), cache={})
    assert res.status == "resolved" and res.almacen_uuids == (UUID,)
    assert res.matched_by.startswith("expediente")


@respx.mock
async def test_resolve_pattern_b_inline() -> None:
    portal = "https://www.mptfp.gob.es/portal/delegaciones_gobierno/delegaciones/comunidad_valenciana/proyectos-ci/info.html"
    listing = f'<li>Expte. PFot-761 (2022/10).</li> <li>OBJETO: utilidad publica</li> <a href="https://{ALM}">descarga</a>'
    respx.get(portal).mock(return_value=httpx.Response(200, text=listing))
    async with httpx.AsyncClient() as c:
        res = await resolve(c, portal, {"PFOT-761"}, set(), cache={})
    assert res.status == "resolved" and res.almacen_uuids == (UUID,) and res.matched_by == "inline"


@respx.mock
async def test_resolve_unresolved() -> None:
    portal = "https://mpt.gob.es/delegaciones_gobierno/delegaciones/navarra/proyectos-ci/"
    # generic landing, no expediente, no almacen, no project sublink
    respx.get(portal).mock(return_value=httpx.Response(200, text="<a href='https://mpt.gob.es/otra-region'>Andalucía</a>"))
    async with httpx.AsyncClient() as c:
        res = await resolve(c, portal, {"ALM-180"}, {"NONEXISTENT"}, cache={})
    assert res.status == "unresolved" and res.almacen_uuids == ()


@respx.mock
async def test_resolve_office_only() -> None:
    portal = "https://mptmd.gob.es/portal/delegaciones_gobierno/delegaciones/x/proyectos-ci/proyectos"
    proj = portal + "/77_proyecto_eolico_x"
    listing = f'<a href="{proj}">EXP-77 Parque eolico X de 50 MW potencia instalada</a>'
    respx.get(portal).mock(return_value=httpx.Response(200, text=listing))
    respx.get(proj).mock(return_value=httpx.Response(200, text="Documentación disponible mediante cita previa en la oficina."))
    async with httpx.AsyncClient() as c:
        res = await resolve(c, portal, {"EXP-77"}, set(), cache={})
    assert res.status == "office_only" and res.almacen_uuids == ()


@respx.mock
async def test_resolve_pattern_c_province_navigation() -> None:
    """Castilla-La Mancha: regional index → province sub-page (by province name)
    → project page → almacen."""
    portal = "https://mptmd.gob.es/portal/delegaciones_gobierno/delegaciones/castillalamancha/proyectos-ci/informacion-publica"
    province_pg = portal + "/albacete"
    proj = province_pg + "/AB-PFot-ALM-180-El_Cuco"
    # regional index: a province link (text just "Albacete") + an unrelated other-region nav link
    regional = (f'<a href="{province_pg}">Albacete</a>'
                '<a href="https://mptmd.gob.es/portal/delegaciones_gobierno/delegaciones/andalucia/x">Andalucía</a>')
    province_html = f'<a href="{proj}">AB-PFot-ALM-180 El Cuco 33,6 MW de potencia</a>'
    project_html = f'cita previa. Documentación: <a href="https://{ALM}">descargar ZIP</a>'
    respx.get(portal).mock(return_value=httpx.Response(200, text=regional))
    respx.get(province_pg).mock(return_value=httpx.Response(200, text=province_html))
    respx.get(proj).mock(return_value=httpx.Response(200, text=project_html))
    async with httpx.AsyncClient() as c:
        res = await resolve(c, portal, {"ALM-180"}, set(), {"ALBACETE"}, cache={})
    assert res.status == "resolved" and res.almacen_uuids == (UUID,)
    assert res.project_url.endswith("AB-PFot-ALM-180-El_Cuco")

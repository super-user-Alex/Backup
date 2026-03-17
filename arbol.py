"""
ocg_hierarchy_bbox.py
=====================
Deduce la jerarquía padre-hijo entre OCGs comparando sus bounding boxes.

Lógica:
    Si bbox(A) contiene a bbox(B)  →  B es hijo de A.
    El padre de B es el OCG con bbox contenedor MÁS PEQUEÑO
    (el contenedor más ajustado), para evitar que un OCG raíz
    que abarca todo sea padre de todos.

Dependencias:
    pip install pikepdf pymupdf
"""

import os
import tempfile
import fitz
import pikepdf
from pikepdf import Pdf, Array


# ── Reutilizamos helpers de mover_pdf_directo.py ─────────────────────────────
# Si no los tienes en el path, cópialos aquí o importa el módulo.
try:
    from mover_pdf_directo import (
        _ocg_objgen_a_nombre,
        _leer_estados_originales,
        _encender_solo,
        _restaurar_estados,
    )
except ImportError:
    # ── Copias mínimas por si no está el módulo ───────────────────────────────
    def _ocg_objgen_a_nombre(pdf):
        r = {}
        try:
            for ref in pdf.Root["/OCProperties"]["/OCGs"]:
                r[ref.objgen] = str(ref["/Name"])
        except Exception:
            pass
        return r

    def _leer_estados_originales(pdf):
        estados = {}
        try:
            oc  = pdf.Root["/OCProperties"]
            off = set()
            if "/OFF" in oc["/D"]:
                for r in oc["/D"]["/OFF"]:
                    if hasattr(r, "objgen"):
                        off.add(r.objgen)
            for ref in oc["/OCGs"]:
                estados[str(ref["/Name"])] = ref.objgen not in off
        except Exception:
            pass
        return estados

    def _encender_solo(pdf, nombre):
        try:
            oc = pdf.Root["/OCProperties"]
            off_list = [r for r in oc["/OCGs"] if str(r["/Name"]) != nombre]
            oc["/D"]["/OFF"] = Array(off_list)
        except Exception:
            pass

    def _restaurar_estados(pdf, estados):
        try:
            oc = pdf.Root["/OCProperties"]
            off_list = [r for r in oc["/OCGs"] if not estados.get(str(r["/Name"]), True)]
            oc["/D"]["/OFF"] = Array(off_list)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Obtener bbox de cada OCG
# ─────────────────────────────────────────────────────────────────────────────

def _bbox_ocg(pdf: Pdf, nombre: str, estados_orig: dict) -> tuple | None:
    """
    Devuelve (x0, y0, x1, y1) en coordenadas top-down (fitz),
    o None si el OCG no tiene elementos medibles.
    """
    _encender_solo(pdf, nombre)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    pdf.save(tmp.name)
    _restaurar_estados(pdf, estados_orig)

    doc   = fitz.open(tmp.name)
    paths = doc[0].get_drawings()
    doc.close()
    os.unlink(tmp.name)

    if not paths:
        return None

    x0 = min(p["rect"].x0 for p in paths)
    y0 = min(p["rect"].y0 for p in paths)
    x1 = max(p["rect"].x1 for p in paths)
    y1 = max(p["rect"].y1 for p in paths)
    return (x0, y0, x1, y1)


def obtener_bboxes(pdf_path: str, tolerancia: float = 2.0) -> dict:
    """
    Devuelve {nombre_ocg: (x0, y0, x1, y1)} para todos los OCGs.
    tolerancia: margen en pts para considerar un bbox como contenedor.
    """
    pdf          = Pdf.open(pdf_path)
    ogmap        = _ocg_objgen_a_nombre(pdf)
    estados_orig = _leer_estados_originales(pdf)
    bboxes       = {}

    print(f"\n  Midiendo bboxes ({len(ogmap)} OCGs)...")
    for nombre in ogmap.values():
        bb = _bbox_ocg(pdf, nombre, estados_orig)
        if bb:
            bboxes[nombre] = bb
            print(f"    {nombre:30s}  x={bb[0]:.1f} y={bb[1]:.1f}  "
                  f"w={bb[2]-bb[0]:.1f} h={bb[3]-bb[1]:.1f}")
        else:
            print(f"    {nombre:30s}  (sin elementos)")

    pdf.close()
    return bboxes


# ─────────────────────────────────────────────────────────────────────────────
# 2. Deducir jerarquía por contención
# ─────────────────────────────────────────────────────────────────────────────

def _contiene(padre: tuple, hijo: tuple, tol: float = 2.0) -> bool:
    """
    True si bbox padre contiene a bbox hijo (con margen de tolerancia).
    Un bbox no se contiene a sí mismo (requiere que sea estrictamente mayor).
    """
    px0, py0, px1, py1 = padre
    hx0, hy0, hx1, hy1 = hijo

    # El padre debe ser más grande en al menos un lado
    mismo = (abs(px0 - hx0) < tol and abs(py0 - hy0) < tol and
             abs(px1 - hx1) < tol and abs(py1 - hy1) < tol)
    if mismo:
        return False

    return (px0 - tol <= hx0 and
            py0 - tol <= hy0 and
            px1 + tol >= hx1 and
            py1 + tol >= hy1)


def _area(bbox: tuple) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def deducir_jerarquia(bboxes: dict, tolerancia: float = 2.0) -> dict:
    """
    Deduce padre-hijo por contención de bboxes.

    Para cada OCG B, su padre es el OCG A tal que:
      - bbox(A) contiene a bbox(B)
      - area(A) es la MENOR entre todos los contenedores de B
        (el contenedor más ajustado = padre más directo)

    Devuelve {nombre: nombre_padre | None}
    """
    nombres = list(bboxes.keys())
    padre   = {n: None for n in nombres}

    for hijo in nombres:
        bb_hijo        = bboxes[hijo]
        mejor_padre    = None
        mejor_area     = float("inf")

        for candidato in nombres:
            if candidato == hijo:
                continue
            bb_cand = bboxes[candidato]
            if _contiene(bb_cand, bb_hijo, tolerancia):
                a = _area(bb_cand)
                if a < mejor_area:
                    mejor_area  = a
                    mejor_padre = candidato

        padre[hijo] = mejor_padre

    return padre


def construir_arbol(padre: dict) -> dict:
    """
    Convierte {hijo: padre} en {padre: [hijos]} (árbol de listas).
    Los nodos sin padre son raíces.
    """
    arbol: dict = {n: [] for n in padre}
    for hijo, p in padre.items():
        if p is not None:
            arbol[p].append(hijo)
    return arbol


# ─────────────────────────────────────────────────────────────────────────────
# 3. Imprimir árbol
# ─────────────────────────────────────────────────────────────────────────────

def _imprimir_nodo(nombre: str, arbol: dict, bboxes: dict,
                   estados: dict, prefijo: str = "", es_ultimo: bool = True):
    rama    = "└── " if es_ultimo else "├── "
    sangria = prefijo + ("    " if es_ultimo else "│   ")

    hijos   = arbol.get(nombre, [])
    bb      = bboxes.get(nombre)
    on      = estados.get(nombre, True)
    icono   = "🟢" if on else "⚫"
    carpeta = "📂" if hijos else "  "

    bbox_str = (f"  [{bb[0]:.0f},{bb[1]:.0f} → {bb[2]:.0f},{bb[3]:.0f}  "
                f"{bb[2]-bb[0]:.0f}×{bb[3]-bb[1]:.0f}pt]") if bb else ""

    print(f"{prefijo}{rama}{carpeta} {icono} {nombre}{bbox_str}")

    for i, hijo in enumerate(sorted(hijos)):
        _imprimir_nodo(hijo, arbol, bboxes, estados,
                       sangria, es_ultimo=(i == len(hijos) - 1))


def ver_arbol_por_bbox(pdf_path: str, tolerancia: float = 2.0,
                       mostrar_bbox: bool = True):
    """
    Construye y muestra la jerarquía de OCGs deducida por contención de bboxes.

    Args:
        tolerancia  : margen en pts para considerar contención (default 2.0).
                      Súbelo si OCGs hermanos se solapan ligeramente.
        mostrar_bbox: muestra las coordenadas junto a cada nodo.

    Leyenda:
        🟢 visible   ⚫ oculto   📂 tiene hijos
    """
    # Bboxes
    bboxes = obtener_bboxes(pdf_path, tolerancia)
    if not bboxes:
        print("  Ningún OCG tiene elementos medibles.")
        return

    # Estados de visibilidad
    pdf          = Pdf.open(pdf_path)
    estados_orig = _leer_estados_originales(pdf)
    pdf.close()

    # Jerarquía
    padre = deducir_jerarquia(bboxes, tolerancia)
    arbol = construir_arbol(padre)
    raices = [n for n, p in padre.items() if p is None]

    print(f"\n{'═' * 60}")
    print(f"  Jerarquía por bbox: {pdf_path}")
    print(f"  tolerancia={tolerancia}pt   OCGs con bbox={len(bboxes)}")
    print(f"{'═' * 60}")

    if not mostrar_bbox:
        bboxes_param = {k: None for k in bboxes}
    else:
        bboxes_param = bboxes

    for i, raiz in enumerate(sorted(raices)):
        _imprimir_nodo(raiz, arbol, bboxes_param, estados_orig,
                       "  ", es_ultimo=(i == len(raices) - 1))

    # OCGs sin bbox (sin elementos)
    pdf2  = Pdf.open(pdf_path)
    ogmap = _ocg_objgen_a_nombre(pdf2)
    pdf2.close()
    sin_bbox = [n for n in ogmap.values() if n not in bboxes]
    if sin_bbox:
        print(f"\n  ── Sin elementos medibles ({'─'*30})")
        for n in sin_bbox:
            print(f"     • {n}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    ver_arbol_por_bbox(PDF)

    # Si hay OCGs hermanos que se solapan y aparecen como hijos,
    # aumenta la tolerancia negativa para ser más estricto:
    # ver_arbol_por_bbox(PDF, tolerancia=-5.0)

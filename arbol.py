"""
ocg_elements.py
===============
Como Illustrator solo exporta OCGs a nivel raíz, los sub-grupos se pierden.
Este script analiza los elementos (paths, imágenes, texto) dentro de cada OCG
y los agrupa espacialmente para inferir sub-grupos.

Estrategia:
    1. Para cada OCG, obtener todos sus elementos con sus bboxes.
    2. Agrupar elementos por proximidad / contención (clustering por gap).
    3. Mostrar cuántos elementos hay y qué sub-grupos se detectan.

Dependencias:
    pip install pikepdf pymupdf
"""

import os
import tempfile
import fitz
import pikepdf
from pikepdf import Pdf, Array


# ── Helpers (copias mínimas si no está mover_pdf_directo.py) ─────────────────
try:
    from mover_pdf_directo import (
        _ocg_objgen_a_nombre,
        _leer_estados_originales,
        _encender_solo,
        _restaurar_estados,
    )
except ImportError:
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
            off_list = [r for r in oc["/OCGs"]
                        if not estados.get(str(r["/Name"]), True)]
            oc["/D"]["/OFF"] = Array(off_list)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Obtener elementos de un OCG
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_solo_ocg(pdf: Pdf, nombre: str, estados_orig: dict) -> str:
    """Guarda un PDF temporal con solo ese OCG visible. Devuelve la ruta."""
    _encender_solo(pdf, nombre)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    pdf.save(tmp.name)
    _restaurar_estados(pdf, estados_orig)
    return tmp.name


def _obtener_elementos(tmp_path: str) -> list[dict]:
    """
    Extrae todos los elementos de la página y devuelve lista de dicts:
        { type, bbox, area, color }
    Tipos: 'path', 'image', 'text'
    """
    doc      = fitz.open(tmp_path)
    page     = doc[0]
    elementos = []

    # Paths y formas vectoriales
    for p in page.get_drawings():
        r = p["rect"]
        if r.width < 0.5 and r.height < 0.5:
            continue   # puntos/artefactos
        color = p.get("color") or p.get("fill")
        elementos.append({
            "type" : "path",
            "bbox" : (r.x0, r.y0, r.x1, r.y1),
            "area" : r.width * r.height,
            "color": color,
        })

    # Imágenes
    for img in page.get_image_info(xrefs=True):
        r = fitz.Rect(img["bbox"])
        elementos.append({
            "type" : "image",
            "bbox" : (r.x0, r.y0, r.x1, r.y1),
            "area" : r.width * r.height,
            "color": None,
        })

    # Texto
    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        r = fitz.Rect(blk["bbox"])
        texto = " ".join(
            span["text"]
            for line in blk.get("lines", [])
            for span in line.get("spans", [])
        ).strip()
        if texto:
            elementos.append({
                "type" : "text",
                "bbox" : (r.x0, r.y0, r.x1, r.y1),
                "area" : r.width * r.height,
                "color": None,
                "text" : texto[:60],
            })

    doc.close()
    return elementos


# ─────────────────────────────────────────────────────────────────────────────
# 2. Clustering espacial por gap
# ─────────────────────────────────────────────────────────────────────────────

def _bbox_union(elementos: list[dict]) -> tuple:
    x0 = min(e["bbox"][0] for e in elementos)
    y0 = min(e["bbox"][1] for e in elementos)
    x1 = max(e["bbox"][2] for e in elementos)
    y1 = max(e["bbox"][3] for e in elementos)
    return (x0, y0, x1, y1)


def _distancia(a: tuple, b: tuple) -> float:
    """Distancia mínima entre dos bboxes (0 si se solapan)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(0, max(ax0, bx0) - min(ax1, bx1))
    dy = max(0, max(ay0, by0) - min(ay1, by1))
    return (dx**2 + dy**2) ** 0.5


def clustering_por_gap(elementos: list[dict], gap: float) -> list[list[dict]]:
    """
    Agrupa elementos cuya distancia mutua es <= gap.
    Algoritmo: union-find sobre pares cercanos.
    """
    n       = len(elementos)
    padre   = list(range(n))

    def find(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    def union(x, y):
        padre[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if _distancia(elementos[i]["bbox"], elementos[j]["bbox"]) <= gap:
                union(i, j)

    grupos: dict = {}
    for i in range(n):
        raiz = find(i)
        grupos.setdefault(raiz, []).append(elementos[i])

    # Ordenar grupos por posición top-left de su bbox unión
    resultado = list(grupos.values())
    resultado.sort(key=lambda g: (_bbox_union(g)[1], _bbox_union(g)[0]))
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# 3. Análisis completo de un OCG
# ─────────────────────────────────────────────────────────────────────────────

def analizar_ocg(pdf_path: str, nombre_ocg: str,
                 gap: float = 20.0) -> dict:
    """
    Analiza los elementos de un OCG y devuelve:
        {
          total     : int,
          bbox      : (x0,y0,x1,y1),
          por_tipo  : {path: N, image: N, text: N},
          grupos    : [ {bbox, elementos:[...]} ]
        }

    gap: distancia máxima en pts para considerar elementos del mismo sub-grupo.
    """
    pdf          = Pdf.open(pdf_path)
    ogmap        = _ocg_objgen_a_nombre(pdf)
    estados_orig = _leer_estados_originales(pdf)

    if nombre_ocg not in ogmap.values():
        pdf.close()
        raise ValueError(f"OCG '{nombre_ocg}' no encontrado. "
                         f"Disponibles: {list(ogmap.values())}")

    tmp = _pdf_solo_ocg(pdf, nombre_ocg, estados_orig)
    pdf.close()

    elementos = _obtener_elementos(tmp)
    os.unlink(tmp)

    if not elementos:
        return {"total": 0, "bbox": None, "por_tipo": {}, "grupos": []}

    por_tipo = {}
    for e in elementos:
        por_tipo[e["type"]] = por_tipo.get(e["type"], 0) + 1

    grupos_raw = clustering_por_gap(elementos, gap)
    grupos = [
        {
            "bbox"     : _bbox_union(g),
            "total"    : len(g),
            "por_tipo" : {t: sum(1 for e in g if e["type"] == t)
                          for t in set(e["type"] for e in g)},
            "elementos": g,
        }
        for g in grupos_raw
    ]

    return {
        "total"   : len(elementos),
        "bbox"    : _bbox_union(elementos),
        "por_tipo": por_tipo,
        "grupos"  : grupos,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Analizar todos los OCGs
# ─────────────────────────────────────────────────────────────────────────────

def analizar_todos(pdf_path: str, gap: float = 20.0,
                   mostrar_elementos: bool = False):
    """
    Analiza cada OCG y muestra su desglose de elementos y sub-grupos.

    Args:
        gap               : distancia en pts para agrupar (default 20).
        mostrar_elementos : si True, lista cada elemento individual.
    """
    pdf    = Pdf.open(pdf_path)
    ogmap  = _ocg_objgen_a_nombre(pdf)
    estados_orig = _leer_estados_originales(pdf)
    pdf.close()

    print(f"\n{'═' * 65}")
    print(f"  Análisis de elementos por OCG: {pdf_path}")
    print(f"  gap={gap}pt")
    print(f"{'═' * 65}")

    resultados = {}
    for nombre in ogmap.values():
        info = analizar_ocg(pdf_path, nombre, gap)
        resultados[nombre] = info

        on    = estados_orig.get(nombre, True)
        icono = "🟢" if on else "⚫"
        total = info["total"]

        if total == 0:
            print(f"\n  {icono} {nombre}  — sin elementos")
            continue

        bb  = info["bbox"]
        pt  = info["por_tipo"]
        gs  = info["grupos"]

        tipo_str = "  ".join(f"{t}:{n}" for t, n in sorted(pt.items()))
        print(f"\n  {icono} {nombre}")
        print(f"     elementos : {total}  ({tipo_str})")
        print(f"     bbox      : x={bb[0]:.0f} y={bb[1]:.0f}  "
              f"{bb[2]-bb[0]:.0f}×{bb[3]-bb[1]:.0f} pt")

        if len(gs) == 1:
            print(f"     sub-grupos: 1  (todos contiguos)")
        else:
            print(f"     sub-grupos: {len(gs)}  (gap={gap}pt)")
            for i, g in enumerate(gs, 1):
                bb2     = g["bbox"]
                pt2     = g["por_tipo"]
                tipo2   = "  ".join(f"{t}:{n}" for t, n in sorted(pt2.items()))
                print(f"       [{i}] {g['total']} elem  ({tipo2})"
                      f"  x={bb2[0]:.0f} y={bb2[1]:.0f}"
                      f"  {bb2[2]-bb2[0]:.0f}×{bb2[3]-bb2[1]:.0f} pt")

                if mostrar_elementos:
                    for e in g["elementos"]:
                        eb = e["bbox"]
                        extra = f"  \"{e.get('text','')}\"" if e["type"] == "text" else ""
                        print(f"           · {e['type']:5s}"
                              f"  x={eb[0]:.0f} y={eb[1]:.0f}"
                              f"  {eb[2]-eb[0]:.0f}×{eb[3]-eb[1]:.0f}{extra}")

    print()
    return resultados


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    # Ver todos los OCGs con sus sub-grupos detectados
    analizar_todos(PDF, gap=20.0)

    # Más detalle: listar cada elemento individual
    # analizar_todos(PDF, gap=20.0, mostrar_elementos=True)

    # Ajustar gap si los sub-grupos no tienen sentido:
    #   gap pequeño (5-10)  → más sub-grupos, más granular
    #   gap grande (50-100) → menos sub-grupos, agrupa más cosas juntas

    # Analizar un solo OCG
    # info = analizar_ocg(PDF, "dec", gap=20.0)
    # print(info)

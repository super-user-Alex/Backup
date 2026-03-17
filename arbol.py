"""
ocg_transform_subgroup.py  (v2)
================================
Aplica traslación y/o rotación a un sub-grupo por su ÍNDICE
(el número [1], [2], [3]... que muestra analizar_todos()).

Enfoque:
    1. Llama a analizar_ocg() para obtener los elementos del sub-grupo N.
    2. Cada elemento tiene su bbox en coords fitz (top-down).
    3. Convierte esos bboxes a coords PDF (bottom-up).
    4. Parsea el BDC...EMC del OCG dividiendo en segmentos de path
       (todo lo que va entre operadores de paint: f, S, B, n…).
    5. Calcula el bbox de cada segmento y comprueba si coincide con
       algún elemento del sub-grupo.
    6. Envuelve los segmentos coincidentes con  q / cm / Q.

Dependencias:
    pip install pikepdf pymupdf
    + ocg_elements.py en el mismo directorio
"""

import re
import math
import pikepdf
from pikepdf import Pdf, Array

from ocg_elements import analizar_ocg   # usa el clustering ya hecho


# ── Helpers ───────────────────────────────────────────────────────────────────
try:
    from mover_pdf_directo import (
        _ocg_objgen_a_nombre,
        _leer_estados_originales,
        _nombre_ocg_a_alias,
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
        return {}

    def _nombre_ocg_a_alias(page_obj, ogmap):
        resultado = {}
        try:
            props = page_obj["/Resources"]["/Properties"]
            for key in props.keys():
                alias = key.lstrip("/")
                try:
                    og = props[key].objgen
                    if og in ogmap:
                        resultado.setdefault(ogmap[og], []).append(alias)
                except AttributeError:
                    pass
        except Exception:
            pass
        return resultado


# ─────────────────────────────────────────────────────────────────────────────
# Coordenadas fitz ↔ PDF
# ─────────────────────────────────────────────────────────────────────────────

def _fitz_a_pdf_bbox(bbox_fitz, altura):
    x0, y0, x1, y1 = bbox_fitz
    return (x0, altura - y1, x1, altura - y0)


# ─────────────────────────────────────────────────────────────────────────────
# Parseo de segmentos del stream
# ─────────────────────────────────────────────────────────────────────────────

# Operadores que terminan un path
PAINT_OPS = {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*",
             b"b", b"b*", b"n", b"W", b"W*"}


def _segmentar_stream(bloque: bytes) -> list[dict]:
    """
    Divide el bloque en segmentos.  Cada segmento es todo lo que va
    desde el token siguiente al paint anterior hasta el próximo paint
    (incluido).  Devuelve:
        [{ 'raw': bytes, 'bbox': tuple|None }]
    """
    # Tokenizar conservando offsets
    tokens = list(re.finditer(
        rb'(?:'
        rb'\((?:[^()\\]|\\.)*\)'    # string (...)
        rb'|<[0-9A-Fa-f\s]*>'       # hex <...>
        rb'|/\S+'                   # nombre /Foo
        rb'|[^\s\[\]<>()/{}%]+'     # número u operador
        rb')',
        bloque
    ))

    segmentos  = []
    seg_inicio = 0   # offset en bloque donde empieza el segmento actual
    operandos  = []  # valores numéricos acumulados en el segmento

    for m in tokens:
        tok = m.group(0)

        if tok in PAINT_OPS:
            # Fin del segmento: desde seg_inicio hasta el fin de este token
            seg_raw  = bloque[seg_inicio : m.end()]
            bbox     = _bbox_segmento(operandos, tok)
            segmentos.append({"raw": seg_raw, "bbox": bbox})
            seg_inicio = m.end()
            # Saltar blancos entre segmentos
            while seg_inicio < len(bloque) and bloque[seg_inicio:seg_inicio+1] in (b' ', b'\t', b'\r', b'\n'):
                seg_inicio += 1
            operandos = []
        else:
            try:
                operandos.append(float(tok))
            except ValueError:
                operandos.append(tok)   # operador de path o nombre

    # Resto sin paint (comentarios, estado gráfico, etc.)
    cola = bloque[seg_inicio:]
    if cola.strip():
        segmentos.append({"raw": cola, "bbox": None})

    return segmentos


def _bbox_segmento(operandos: list, paint_op: bytes) -> tuple | None:
    """
    Extrae coordenadas de los operandos acumulados y devuelve el bbox.
    Soporta: m l c v y h re  (los más comunes en Illustrator).
    """
    coords = []
    nums   = []

    for op in operandos:
        if isinstance(op, float):
            nums.append(op)
        elif isinstance(op, bytes):
            if op == b"re" and len(nums) >= 4:
                x, y, w, h = nums[-4], nums[-3], nums[-2], nums[-1]
                coords += [x, y, x + w, y + h]
                nums = []
            elif op in (b"m", b"l") and len(nums) >= 2:
                coords += [nums[-2], nums[-1]]
                nums = []
            elif op == b"c" and len(nums) >= 6:
                coords += nums[-6:]
                nums = []
            elif op in (b"v", b"y") and len(nums) >= 4:
                coords += nums[-4:]
                nums = []
            elif op == b"h":
                pass
            else:
                nums = []

    if not coords:
        return None

    xs = coords[0::2]
    ys = coords[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


# ─────────────────────────────────────────────────────────────────────────────
# Coincidencia bbox
# ─────────────────────────────────────────────────────────────────────────────

def _bbox_coincide(seg_bbox: tuple, elem_bboxes_pdf: list[tuple],
                   tol: float) -> bool:
    """
    True si seg_bbox coincide con alguno de los bboxes de elemento.
    Primero prueba coincidencia exacta (con tol), luego contención.
    """
    sx0, sy0, sx1, sy1 = seg_bbox
    for ex0, ey0, ex1, ey1 in elem_bboxes_pdf:
        # Coincidencia directa
        if (abs(sx0 - ex0) <= tol and abs(sy0 - ey0) <= tol and
                abs(sx1 - ex1) <= tol and abs(sy1 - ey1) <= tol):
            return True
        # El segmento está contenido en el elemento
        if (ex0 - tol <= sx0 and ey0 - tol <= sy0 and
                ex1 + tol >= sx1 and ey1 + tol >= sy1):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Matriz cm
# ─────────────────────────────────────────────────────────────────────────────

def _matriz_cm(dx: float, dy: float, angulo_deg: float,
               pivot_x: float, pivot_y: float) -> bytes:
    """
    Genera el operador cm para rotar alrededor del pivot y trasladar.
    dy ya está en coords PDF (bottom-up).
    """
    θ   = math.radians(angulo_deg)
    cos = math.cos(θ)
    sin = math.sin(θ)
    e   = (1 - cos) * pivot_x + sin * pivot_y + dx
    f   = (1 - cos) * pivot_y - sin * pivot_x + dy
    return f"{cos:.6f} {sin:.6f} {-sin:.6f} {cos:.6f} {e:.6f} {f:.6f} cm".encode()


# ─────────────────────────────────────────────────────────────────────────────
# Modificar el bloque BDC...EMC
# ─────────────────────────────────────────────────────────────────────────────

def _modificar_bloque(bloque_interior: bytes,
                      elem_bboxes_pdf: list[tuple],
                      cm_bytes: bytes,
                      tol: float) -> bytes:
    """
    Envuelve con q/cm/Q los segmentos del bloque cuyo bbox coincide
    con alguno de los bboxes de elemento del sub-grupo.
    """
    segmentos = _segmentar_stream(bloque_interior)

    coincidentes = sum(
        1 for s in segmentos
        if s["bbox"] and _bbox_coincide(s["bbox"], elem_bboxes_pdf, tol)
    )
    print(f"    Segmentos totales  : {len(segmentos)}")
    print(f"    Segmentos del sub-grupo: {coincidentes}")

    if coincidentes == 0:
        return bloque_interior

    partes = []
    for seg in segmentos:
        if seg["bbox"] and _bbox_coincide(seg["bbox"], elem_bboxes_pdf, tol):
            partes.append(b"q\n" + cm_bytes + b"\n" + seg["raw"] + b"\nQ\n")
        else:
            partes.append(seg["raw"])

    return b"".join(partes)


# ─────────────────────────────────────────────────────────────────────────────
# Función principal
# ─────────────────────────────────────────────────────────────────────────────

def transformar_subgrupo(
    pdf_path        : str,
    nombre_ocg      : str,
    indice_subgrupo : int,          # número [1],[2],[3]… del listado
    dx              : float = 0.0,  # traslación horizontal en pts (+ = derecha)
    dy_topdown      : float = 0.0,  # traslación top-down (+ = abajo)
    angulo_deg      : float = 0.0,  # rotación en grados (+ = antihorario PDF)
    gap             : float = 20.0, # mismo gap que usaste en analizar_todos()
    output_pdf      : str   = "resultado.pdf",
    tolerancia      : float = 2.0,
) -> str:
    """
    Transforma el sub-grupo Nº indice_subgrupo del OCG indicado.

    El índice es el mismo [1], [2], [3]... que aparece en analizar_todos().
    El pivote de rotación es el centro del bbox del sub-grupo.

    Ejemplo:
        # analizar_todos() mostró:
        #   🟢 dec
        #     sub-grupos: 3
        #       [1] 18 elem  x=10 y=20  280×350 pt
        #       [2] 14 elem  x=300 y=20  280×350 pt

        transformar_subgrupo("diseño.pdf", "dec", indice_subgrupo=1,
                              dx=100, dy_topdown=0, angulo_deg=45)
    """
    # ── 1. Obtener elementos del sub-grupo vía ocg_elements ──────────────────
    print(f"\n  Analizando OCG '{nombre_ocg}' (gap={gap})...")
    info = analizar_ocg(pdf_path, nombre_ocg, gap=gap)

    if not info["grupos"]:
        raise ValueError(f"OCG '{nombre_ocg}' no tiene sub-grupos detectables.")

    n_grupos = len(info["grupos"])
    if not (1 <= indice_subgrupo <= n_grupos):
        raise ValueError(
            f"Índice {indice_subgrupo} fuera de rango. "
            f"El OCG '{nombre_ocg}' tiene {n_grupos} sub-grupo(s)."
        )

    grupo    = info["grupos"][indice_subgrupo - 1]   # 1-based → 0-based
    elementos = grupo["elementos"]
    print(f"  Sub-grupo [{indice_subgrupo}]: {grupo['total']} elementos  "
          f"bbox={tuple(round(x,1) for x in grupo['bbox'])}")

    # ── 2. Obtener altura de página para conversión de coords ────────────────
    pdf      = Pdf.open(pdf_path)
    page_obj = pdf.pages[0]
    mb       = page_obj.obj.get("/MediaBox") or page_obj.obj.get("/CropBox")
    altura   = float(mb[3]) if mb else 842.0

    # ── 3. Convertir bboxes de elementos a coords PDF (bottom-up) ───────────
    elem_bboxes_pdf = [
        _fitz_a_pdf_bbox(e["bbox"], altura)
        for e in elementos
    ]
    print(f"  Bboxes en PDF (bottom-up): {len(elem_bboxes_pdf)} elementos")

    # ── 4. Calcular pivot (centro del bbox del sub-grupo en PDF) ─────────────
    bb_sg_pdf  = _fitz_a_pdf_bbox(grupo["bbox"], altura)
    pivot_x    = (bb_sg_pdf[0] + bb_sg_pdf[2]) / 2
    pivot_y    = (bb_sg_pdf[1] + bb_sg_pdf[3]) / 2
    dy_pdf     = -dy_topdown   # invertir Y

    cm_bytes   = _matriz_cm(dx, dy_pdf, angulo_deg, pivot_x, pivot_y)
    print(f"  Operador cm: {cm_bytes.decode()}")

    # ── 5. Localizar alias del OCG ───────────────────────────────────────────
    ogmap    = _ocg_objgen_a_nombre(pdf)
    n2alias  = _nombre_ocg_a_alias(page_obj.obj, ogmap)
    aliases  = n2alias.get(nombre_ocg, [])

    if not aliases:
        pdf.close()
        raise ValueError(f"No se encontraron aliases para '{nombre_ocg}' en la página.")

    alias_bytes = [a.encode() for a in aliases]
    print(f"  Aliases: {aliases}")

    # Patrón para encontrar el bloque BDC...EMC del OCG
    patron_ocg = re.compile(
        rb'(/OC\s+/(?:' +
        b"|".join(re.escape(ab) for ab in alias_bytes) +
        rb')\s+BDC)(.*?)(EMC)',
        re.DOTALL
    )

    # ── 6. Modificar streams ─────────────────────────────────────────────────
    contenido = page_obj.obj.get("/Contents")
    objetos   = list(contenido) if isinstance(contenido, pikepdf.Array) else [contenido]

    modificados = 0
    for stream_obj in objetos:
        raw = stream_obj.read_bytes()
        if not any(b"/OC /" + ab in raw for ab in alias_bytes):
            continue

        def _reemplazar(m):
            interior_orig = m.group(2)
            interior_mod  = _modificar_bloque(
                interior_orig, elem_bboxes_pdf, cm_bytes, tolerancia
            )
            return m.group(1) + interior_mod + m.group(3)

        mod = patron_ocg.sub(_reemplazar, raw)
        if mod != raw:
            stream_obj.write(mod)
            modificados += 1

    if modificados == 0:
        print("\n  ⚠️  Sin cambios. Prueba aumentar 'tolerancia' (default=2.0).")
    else:
        print(f"\n  ✅ {modificados} stream(s) modificado(s)")

    pdf.save(output_pdf)
    pdf.close()
    print(f"  Guardado: {output_pdf}")
    return output_pdf


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from ocg_elements import analizar_todos

    PDF = "diseño.pdf"

    # Paso 1: ver los sub-grupos disponibles
    analizar_todos(PDF, gap=20.0)

    # Paso 2: transformar el sub-grupo [1] del OCG "dec"
    transformar_subgrupo(
        pdf_path        = PDF,
        nombre_ocg      = "dec",
        indice_subgrupo = 1,       # [1] del listado
        dx              = 100,
        dy_topdown      = 0,
        angulo_deg      = 0,
        gap             = 20.0,    # mismo gap que en analizar_todos()
        output_pdf      = "resultado.pdf",
    )

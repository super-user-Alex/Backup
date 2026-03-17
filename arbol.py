"""
ocg_transform_subgroup.py
=========================
Aplica traslación y/o rotación a un sub-grupo de elementos dentro de un OCG.

El sub-grupo se identifica por su bbox (obtenido con ocg_elements.py).
El script parsea el content stream, calcula el bbox de cada path,
identifica cuáles caen dentro del sub-grupo y los envuelve con q/cm/Q.

Matriz de transformación PDF (operador cm):
    [ a  b  0 ]       a=cos  b=sin
    [ c  d  0 ]  →    c=-sin d=cos
    [ e  f  1 ]       e=tx   f=ty

Para rotar alrededor del centro del sub-grupo y luego trasladar:
    1. Mover origen al centro del bbox: -cx, -cy
    2. Rotar θ grados
    3. Mover al destino: cx + dx, cy + dy

Dependencias:
    pip install pikepdf pymupdf
"""

import re
import math
import os
import tempfile
import fitz
import pikepdf
from pikepdf import Pdf, Array


# ── Helpers ───────────────────────────────────────────────────────────────────
try:
    from mover_pdf_directo import (
        _ocg_objgen_a_nombre,
        _leer_estados_originales,
        _encender_solo,
        _restaurar_estados,
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

    def _nombre_ocg_a_alias(page_obj, ogmap):
        resultado = {}
        try:
            props = page_obj["/Resources"]["/Properties"]
            for key in props.keys():
                alias = key.lstrip("/")
                try:
                    og = props[key].objgen
                    if og in ogmap:
                        nombre = ogmap[og]
                        resultado.setdefault(nombre, []).append(alias)
                except AttributeError:
                    pass
        except Exception:
            pass
        return resultado


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parsear paths del content stream con sus bboxes
# ─────────────────────────────────────────────────────────────────────────────

# Operadores que terminan un path (pintan o descartan)
PAINT_OPS = {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*",
             b"b", b"b*", b"n", b"W", b"W*"}

# Operadores de construcción de path
PATH_OPS  = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}

# Operadores que cambian el estado gráfico sin ser paths
NOOP_OPS  = {b"q", b"Q", b"cm", b"w", b"J", b"j", b"M", b"d",
             b"ri", b"i", b"gs", b"cs", b"CS", b"sc", b"SC",
             b"scn", b"SCN", b"g", b"G", b"rg", b"RG", b"k", b"K"}


def _tokenizar(raw: bytes) -> list[bytes]:
    """Tokeniza un content stream en operandos y operadores."""
    return re.findall(
        rb'(?:'
        rb'\((?:[^()\\]|\\.)*\)'      # string literal (...)
        rb'|<[0-9A-Fa-f\s]*>'         # hex string <...>
        rb'|/\S+'                     # nombre PDF
        rb'|[^\s\[\]<>(){}/%]+'       # número / operador
        rb')',
        raw
    )


def _bbox_de_coords(coords: list[float]) -> tuple | None:
    """Calcula el bbox de una lista de coordenadas [x,y, x,y, ...]."""
    if len(coords) < 2:
        return None
    xs = coords[0::2]
    ys = coords[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _expandir_bbox(a: tuple, b: tuple) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]))


def parsear_paths_stream(raw: bytes) -> list[dict]:
    """
    Parsea el stream y devuelve lista de paths con su rango de bytes y bbox:
        [{
            start  : int,   # offset byte inicio (incluyendo operandos)
            end    : int,   # offset byte fin (incluyendo operador de paint)
            bbox   : (x0, y0, x1, y1),   # en coords PDF (bottom-up)
            tokens : [bytes, ...]
        }]

    Nota: coordenadas en espacio PDF (Y crece hacia arriba).
    """
    tokens   = _tokenizar(raw)
    paths    = []
    operandos = []
    coords_path = []    # todas las coordenadas del path actual
    path_tokens = []    # tokens del path actual

    # Reconstruir offsets: mapear índice de token → offset en raw
    # (simplificado: trabajamos con tokens, los offsets se calculan al final)

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # ── Operador de construcción de path ─────────────────────────────────
        if tok in PATH_OPS:
            nums = []
            for op in operandos:
                try:
                    nums.append(float(op))
                except ValueError:
                    pass

            if tok == b"re":
                # re: x y w h → rectángulo
                if len(nums) >= 4:
                    x, y, w, h = nums[-4], nums[-3], nums[-2], nums[-1]
                    coords_path += [x, y, x+w, y, x+w, y+h, x, y+h]
            elif tok == b"m" and len(nums) >= 2:
                coords_path += [nums[-2], nums[-1]]
            elif tok == b"l" and len(nums) >= 2:
                coords_path += [nums[-2], nums[-1]]
            elif tok == b"c" and len(nums) >= 6:
                coords_path += nums[-6:]
            elif tok == b"v" and len(nums) >= 4:
                coords_path += nums[-4:]
            elif tok == b"y" and len(nums) >= 4:
                coords_path += nums[-4:]
            # h no añade coordenadas nuevas

            path_tokens.append(tok)
            operandos = []

        # ── Operador de paint (fin del path) ─────────────────────────────────
        elif tok in PAINT_OPS:
            path_tokens.append(tok)
            if coords_path:
                bb = _bbox_de_coords(coords_path)
                if bb:
                    paths.append({
                        "bbox"  : bb,
                        "tokens": path_tokens[:],
                    })
            coords_path = []
            path_tokens = []
            operandos   = []

        # ── Operador no-path ──────────────────────────────────────────────────
        elif tok in NOOP_OPS:
            # Si había un path pendiente sin paint (raro pero posible), cerrarlo
            if coords_path:
                bb = _bbox_de_coords(coords_path)
                if bb:
                    paths.append({"bbox": bb, "tokens": path_tokens[:]})
                coords_path = []
                path_tokens = []
            operandos = []

        else:
            operandos.append(tok)
            path_tokens.append(tok)

        i += 1

    return paths


# ─────────────────────────────────────────────────────────────────────────────
# 2. Convertir coordenadas PDF ↔ fitz (top-down)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_a_fitz(bbox_pdf: tuple, altura_pagina: float) -> tuple:
    """Convierte bbox PDF (bottom-up) a fitz (top-down)."""
    x0, y0, x1, y1 = bbox_pdf
    return (x0, altura_pagina - y1, x1, altura_pagina - y0)


def _fitz_a_pdf(bbox_fitz: tuple, altura_pagina: float) -> tuple:
    """Convierte bbox fitz (top-down) a PDF (bottom-up)."""
    x0, y0, x1, y1 = bbox_fitz
    return (x0, altura_pagina - y1, x1, altura_pagina - y0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Identificar qué paths pertenecen al sub-grupo
# ─────────────────────────────────────────────────────────────────────────────

def _solapa(a: tuple, b: tuple, tol: float = 1.0) -> bool:
    """True si los dos bboxes se solapan (con tolerancia)."""
    return not (a[2] + tol < b[0] or b[2] + tol < a[0] or
                a[3] + tol < b[1] or b[3] + tol < a[1])


def _contenido_en(bbox_elem: tuple, bbox_grupo: tuple, tol: float = 2.0) -> bool:
    """True si bbox_elem está contenido dentro de bbox_grupo."""
    return (bbox_grupo[0] - tol <= bbox_elem[0] and
            bbox_grupo[1] - tol <= bbox_elem[1] and
            bbox_grupo[2] + tol >= bbox_elem[2] and
            bbox_grupo[3] + tol >= bbox_elem[3])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Matriz de transformación
# ─────────────────────────────────────────────────────────────────────────────

def _matriz_cm(dx: float, dy: float, angulo_deg: float,
               pivot_x: float, pivot_y: float) -> str:
    """
    Genera el operador cm para:
      - Rotar 'angulo_deg' grados alrededor de (pivot_x, pivot_y)
      - Trasladar (dx, dy) en coordenadas PDF (Y hacia arriba)

    La matriz resultante es la composición:
      T(pivot) · R(θ) · T(-pivot) · T(dx, dy)

    Devuelve string listo para insertar en el stream.
    """
    θ   = math.radians(angulo_deg)
    cos = math.cos(θ)
    sin = math.sin(θ)

    # Componentes de traslación compuesta
    e = (1 - cos) * pivot_x + sin * pivot_y + dx
    f = (1 - cos) * pivot_y - sin * pivot_x + dy

    return f"{cos:.6f} {sin:.6f} {-sin:.6f} {cos:.6f} {e:.6f} {f:.6f} cm"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Modificar el stream: envolver paths del sub-grupo con q/cm/Q
# ─────────────────────────────────────────────────────────────────────────────

def _reconstruir_stream(raw: bytes, bbox_subgrupo_pdf: tuple,
                        cm_str: str, tol: float = 2.0) -> bytes:
    """
    Recorre el stream línea a línea. Cuando detecta un bloque de paths
    cuyo bbox cae dentro de bbox_subgrupo_pdf, lo envuelve con q/cm/Q.

    Estrategia conservadora:
      - Parsea paths completos (operandos + operador de paint).
      - Agrupa paths consecutivos que pertenecen al sub-grupo.
      - Inserta q/cm antes del primer path del grupo y Q después del último.
    """
    tokens_stream = _tokenizar(raw)
    paths_info    = parsear_paths_stream(raw)

    # Índice de qué tokens pertenecen a paths del sub-grupo
    # Para eso reconstruimos el stream token a token y marcamos grupos
    pertenece_subgrupo: set[int] = set()

    idx = 0
    for path in paths_info:
        bb_pdf = path["bbox"]
        if _contenido_en(bb_pdf, bbox_subgrupo_pdf, tol):
            # Marcar todos los tokens de este path
            ntok = len(path["tokens"])
            for k in range(idx, idx + ntok):
                pertenece_subgrupo.add(k)
        idx += len(path["tokens"])

    if not pertenece_subgrupo:
        print("    ⚠️  Ningún path del stream coincide con el bbox del sub-grupo.")
        print(f"    bbox sub-grupo (PDF): {tuple(round(x,1) for x in bbox_subgrupo_pdf)}")
        return raw

    # Reconstruir stream insertando q/cm/Q alrededor de los bloques
    lineas_out   = []
    dentro       = False
    translate_in = f"q\n{cm_str}\n".encode()

    # Trabajar a nivel de líneas del stream original para preservar formato
    # Reidentificar qué líneas corresponden a paths del sub-grupo

    # Simplificación: reconstruir el stream desde tokens marcados
    # agrupando tokens consecutivos del sub-grupo
    salida_tokens = []
    i = 0
    toks = tokens_stream  # lista plana de todos los tokens

    # Recalcular qué tokens del stream (índice global) son de sub-grupo
    # usando la misma lógica de parseo
    idx_global = 0
    pertenece_global: set[int] = set()
    operandos_idx: list[int]   = []
    path_tok_idx: list[int]    = []

    for i2, tok in enumerate(toks):
        if tok in PATH_OPS or tok == b"re":
            path_tok_idx.extend(operandos_idx)
            path_tok_idx.append(i2)
            operandos_idx = []
        elif tok in PAINT_OPS:
            path_tok_idx.append(i2)
            # Verificar si este path está en el sub-grupo
            path_toks_vals = [toks[k] for k in path_tok_idx]
            coords = []
            j = 0
            while j < len(path_toks_vals):
                t = path_toks_vals[j]
                if t == b"re":
                    nums = []
                    for k2 in range(j-1, max(j-5, -1), -1):
                        try:
                            nums.insert(0, float(path_toks_vals[k2]))
                        except (ValueError, IndexError):
                            break
                    if len(nums) >= 4:
                        x, y, w, h = nums[0], nums[1], nums[2], nums[3]
                        coords += [x, y, x+w, y, x+w, y+h, x, y+h]
                elif t in (b"m", b"l"):
                    for k2 in range(j-1, max(j-3, -1), -1):
                        try:
                            float(path_toks_vals[k2])
                        except (ValueError, IndexError):
                            break
                j += 1

            bb = _bbox_de_coords(coords) if coords else None
            if bb and _contenido_en(bb, bbox_subgrupo_pdf, tol):
                for k in path_tok_idx:
                    pertenece_global.add(k)

            path_tok_idx  = []
            operandos_idx = []
        elif tok in NOOP_OPS:
            path_tok_idx  = []
            operandos_idx = []
        else:
            operandos_idx.append(i2)
            path_tok_idx.append(i2)

    # Reconstruir el stream con los bloques envueltos
    # Agrupar tokens consecutivos pertenecientes al sub-grupo
    resultado_partes = []
    bloque_actual    = []
    en_subgrupo      = False

    lineas = raw.split(b"\n")
    # Estrategia más robusta: trabajar a nivel de líneas del stream original
    # Identificar líneas que contienen operadores de paths del sub-grupo

    # Reconstruir con líneas
    lineas_subgrupo: set[int] = set()
    idx_tok = 0
    tok_por_linea: list[list[int]] = []
    for li, linea in enumerate(lineas):
        toks_linea = _tokenizar(linea)
        indices    = list(range(idx_tok, idx_tok + len(toks_linea)))
        tok_por_linea.append(indices)
        idx_tok += len(toks_linea)

    for li, indices in enumerate(tok_por_linea):
        if any(idx in pertenece_global for idx in indices):
            lineas_subgrupo.add(li)

    # Insertar q/cm/Q alrededor de rangos continuos de líneas del sub-grupo
    salida: list[bytes] = []
    i = 0
    while i < len(lineas):
        if i in lineas_subgrupo:
            # Encontrar el final del bloque contiguo
            j = i
            while j < len(lineas) and j in lineas_subgrupo:
                j += 1
            # Insertar apertura, líneas del bloque, cierre
            salida.append(translate_in.rstrip(b"\n"))
            salida.extend(lineas[i:j])
            salida.append(b"Q")
            i = j
        else:
            salida.append(lineas[i])
            i += 1

    return b"\n".join(salida)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Función principal
# ─────────────────────────────────────────────────────────────────────────────

def transformar_subgrupo(
    pdf_path     : str,
    nombre_ocg   : str,
    bbox_subgrupo: tuple,        # (x0, y0, x1, y1) en coords FITZ (top-down)
    dx           : float = 0.0,  # traslación horizontal en pts
    dy_topdown   : float = 0.0,  # traslación vertical top-down (+ = abajo)
    angulo_deg   : float = 0.0,  # rotación en grados (+ = antihorario en PDF)
    output_pdf   : str   = "resultado.pdf",
    tolerancia   : float = 2.0,
) -> str:
    """
    Aplica traslación y/o rotación a un sub-grupo dentro de un OCG.

    Args:
        nombre_ocg   : Nombre del OCG que contiene el sub-grupo.
        bbox_subgrupo: Bbox del sub-grupo en coords top-down (de ocg_elements.py).
                       Formato: (x0, y0, x1, y1)
        dx           : Desplazamiento horizontal en pts (+ = derecha).
        dy_topdown   : Desplazamiento vertical top-down (+ = abajo).
        angulo_deg   : Rotación en grados. Positivo = antihorario (conv. PDF).
                       El pivote de rotación es el centro del sub-grupo.
        tolerancia   : Margen en pts para identificar paths del sub-grupo.

    Ejemplo:
        transformar_subgrupo(
            pdf_path      = "diseño.pdf",
            nombre_ocg    = "dec",
            bbox_subgrupo = (10, 20, 290, 370),   # de analizar_todos()
            dx            = 50,
            dy_topdown    = 0,
            angulo_deg    = 45,
            output_pdf    = "resultado.pdf",
        )
    """
    pdf    = Pdf.open(pdf_path)
    ogmap  = _ocg_objgen_a_nombre(pdf)
    estados_orig = _leer_estados_originales(pdf)

    if nombre_ocg not in ogmap.values():
        pdf.close()
        raise ValueError(f"OCG '{nombre_ocg}' no encontrado.\n"
                         f"Disponibles: {list(ogmap.values())}")

    # Obtener altura de página para conversión de coordenadas
    page_obj    = pdf.pages[0]
    media_box   = page_obj.obj.get("/MediaBox") or page_obj.obj.get("/CropBox")
    altura_pag  = float(media_box[3]) if media_box else 842.0

    # Convertir bbox del sub-grupo de fitz (top-down) a PDF (bottom-up)
    bbox_sg_pdf = _fitz_a_pdf(bbox_subgrupo, altura_pag)
    print(f"\n  Sub-grupo bbox fitz : {tuple(round(x,1) for x in bbox_subgrupo)}")
    print(f"  Sub-grupo bbox PDF  : {tuple(round(x,1) for x in bbox_sg_pdf)}")

    # Centro del sub-grupo en coords PDF (pivot de rotación)
    cx_pdf = (bbox_sg_pdf[0] + bbox_sg_pdf[2]) / 2
    cy_pdf = (bbox_sg_pdf[1] + bbox_sg_pdf[3]) / 2

    # Convertir dy de top-down a PDF (invertir Y)
    dy_pdf = -dy_topdown

    # Construir la cadena cm
    cm_str = _matriz_cm(dx, dy_pdf, angulo_deg, cx_pdf, cy_pdf)
    print(f"  Transformación      : dx={dx} dy_topdown={dy_topdown} "
          f"angulo={angulo_deg}°")
    print(f"  Operador cm         : {cm_str}")

    # Encontrar los alias del OCG en el stream
    nombre_a_aliases = _nombre_ocg_a_alias(page_obj.obj, ogmap)
    aliases          = nombre_a_aliases.get(nombre_ocg, [])

    if not aliases:
        pdf.close()
        raise ValueError(f"OCG '{nombre_ocg}' no tiene alias en el stream de la página.")

    print(f"  Aliases OCG         : {aliases}")

    # Modificar stream
    streams_mod = 0
    contenido   = page_obj.obj.get("/Contents")
    if isinstance(contenido, pikepdf.Array):
        objetos = list(contenido)
    else:
        objetos = [contenido]

    for stream_obj in objetos:
        raw = stream_obj.read_bytes()

        # Solo modificar el stream que contiene el OCG
        alias_bytes = [a.encode() for a in aliases]
        if not any(b"/OC /" + ab in raw for ab in alias_bytes):
            continue

        # Extraer solo el bloque BDC...EMC del OCG
        # para no confundir paths de otros OCGs
        patron_ocg = re.compile(
            rb'/OC\s+/(?:' +
            b'|'.join(re.escape(ab) for ab in alias_bytes) +
            rb')\s+BDC(.*?)EMC',
            re.DOTALL
        )

        def _reemplazar_bloque(m):
            bloque_interior = m.group(1)
            bloque_mod = _reconstruir_stream(
                bloque_interior, bbox_sg_pdf, cm_str, tolerancia
            )
            return (m.group(0)[:m.start(1) - m.start(0)] +
                    bloque_mod +
                    b"\nEMC")

        mod = patron_ocg.sub(_reemplazar_bloque, raw)

        if mod != raw:
            stream_obj.write(mod)
            streams_mod += 1
            print(f"  ✅ Stream modificado")
        else:
            print(f"  ⚠️  Stream sin cambios — ajusta 'tolerancia' o verifica el bbox")

    if streams_mod == 0:
        print("\n  Sugerencia: ejecuta analizar_todos() con mostrar_elementos=True")
        print("  para confirmar las coordenadas exactas del sub-grupo.")

    pdf.save(output_pdf)
    pdf.close()
    print(f"\n  Guardado: {output_pdf}")
    return output_pdf


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    # 1. Primero analiza el OCG para obtener el bbox del sub-grupo:
    #
    #    from ocg_elements import analizar_todos
    #    analizar_todos(PDF, gap=20.0)
    #
    #    Output ejemplo:
    #      🟢 dec
    #        sub-grupos: 3
    #          [1] 18 elem  x=10 y=20  280×350 pt   ← este queremos mover
    #          [2] 14 elem  x=300 y=20  280×350 pt
    #          [3] 10 elem  x=10 y=400  580×380 pt

    # 2. Aplicar transformación al sub-grupo [1]:
    transformar_subgrupo(
        pdf_path      = PDF,
        nombre_ocg    = "dec",
        bbox_subgrupo = (10, 20, 290, 370),   # bbox del sub-grupo [1]
        dx            = 100,                  # 100 pts a la derecha
        dy_topdown    = 50,                   # 50 pts hacia abajo
        angulo_deg    = 0,                    # sin rotación
        output_pdf    = "resultado.pdf",
    )

    # 3. Solo rotación (45° antihorario alrededor del centro del sub-grupo):
    # transformar_subgrupo(
    #     pdf_path      = PDF,
    #     nombre_ocg    = "dec",
    #     bbox_subgrupo = (10, 20, 290, 370),
    #     dx            = 0,
    #     dy_topdown    = 0,
    #     angulo_deg    = 45,
    #     output_pdf    = "resultado.pdf",
    # )

    # 4. Rotación + traslación simultáneas:
    # transformar_subgrupo(
    #     pdf_path      = PDF,
    #     nombre_ocg    = "dec",
    #     bbox_subgrupo = (10, 20, 290, 370),
    #     dx            = 100,
    #     dy_topdown    = -30,    # 30 pts hacia arriba
    #     angulo_deg    = -90,    # 90° horario
    #     output_pdf    = "resultado.pdf",
    # )

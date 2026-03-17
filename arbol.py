"""
ocg_illustrator_layers.py
=========================
Lee la jerarquía de capas tal como Illustrator la tiene internamente,
desde dos fuentes dentro del PDF:

  Fuente 1 — XMP Metadata (/Root/Metadata)
      Illustrator escribe las capas en el namespace xap/ai con atributos
      de orden, visibilidad y bloqueo.

  Fuente 2 — Stream nativo .ai embebido
      Si guardaste con "Preserve Illustrator Editing Capabilities",
      el .ai completo vive dentro del PDF como un stream comprimido.
      Contiene marcadores %AI5_BeginLayer con nombre, visibilidad,
      bloqueo, color de etiqueta y jerarquía real padre-hijo.

Dependencias:
    pip install pikepdf
    (xml.etree.ElementTree viene en stdlib)
"""

import re
import zlib
import pikepdf
from pikepdf import Pdf
import xml.etree.ElementTree as ET


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 1: XMP Metadata
# ─────────────────────────────────────────────────────────────────────────────

# Namespaces que usa Illustrator en el XMP
NS = {
    "x"        : "adobe:ns:meta/",
    "rdf"      : "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc"       : "http://purl.org/dc/elements/1.1/",
    "xap"      : "http://ns.adobe.com/xap/1.0/",
    "pdf"      : "http://ns.adobe.com/pdf/1.3/",
    "ai"       : "http://ns.adobe.com/AdobeIllustrator/10.0/",
    "illustrator": "http://ns.adobe.com/illustrator/1.0/",
    "xapGImg"  : "http://ns.adobe.com/xap/1.0/g/img/",
}


def leer_capas_xmp(pdf_path: str) -> list[dict]:
    """
    Extrae información de capas desde el XMP del PDF.
    Devuelve lista de dicts:
        [{ name, visible, locked, printable, order }, ...]
    ordenada por 'order' si está disponible.
    """
    pdf = Pdf.open(pdf_path)
    try:
        meta_stream = pdf.Root["/Metadata"]
        xmp_bytes   = meta_stream.read_bytes()
    except (KeyError, AttributeError):
        print("  Sin /Metadata en este PDF.")
        pdf.close()
        return []
    pdf.close()

    # Limpiar BOM y namespaces problemáticos para el parser
    xmp_str = xmp_bytes.decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(xmp_str)
    except ET.ParseError as e:
        print(f"  Error parseando XMP: {e}")
        return []

    capas = []

    # Buscar elementos con atributo ai:layer o en el namespace de illustrator
    # Illustrator escribe algo como:
    #   <rdf:Description ai:Layers="...">
    # o bien una estructura anidada con cada capa como nodo

    # Estrategia: buscar cualquier nodo que tenga atributo de nombre de capa
    # Los atributos varían por versión de Illustrator, buscar patrones comunes

    xmp_raw = xmp_str

    # Patrón 1: capas en atributo "stFnt:fontName" style (Illustrator CS+)
    # <illustrator:Layer illustrator:Name="Capa 1" .../>
    patron_capa = re.compile(
        r'<(?:[\w:]+:)?[Ll]ayer\b([^>]*)/>|'
        r'<(?:[\w:]+:)?[Ll]ayer\b([^>]*)>(.*?)</(?:[\w:]+:)?[Ll]ayer>',
        re.DOTALL
    )

    for m in patron_capa.finditer(xmp_raw):
        attrs_raw = m.group(1) or m.group(2) or ""
        nombre    = _extraer_attr(attrs_raw, ["Name", "name", "ai:name",
                                               "illustrator:Name"])
        visible   = _extraer_attr(attrs_raw, ["Visible", "visible"]) or "true"
        locked    = _extraer_attr(attrs_raw, ["Locked", "locked"])   or "false"
        if nombre:
            capas.append({
                "name"    : nombre,
                "visible" : visible.lower() not in ("false", "0"),
                "locked"  : locked.lower()  in  ("true",  "1"),
            })

    if capas:
        print(f"  [XMP] {len(capas)} capas encontradas vía patrón <Layer>")
        return capas

    # Patrón 2: buscar nombres entre etiquetas de cualquier ns con "layer"
    patron_nombre = re.compile(
        r'(?:ai|illustrator|xap):(?:Name|LayerName|layer)["\s]+[=:]\s*["\']([^"\']+)["\']',
        re.IGNORECASE
    )
    nombres = patron_nombre.findall(xmp_raw)
    if nombres:
        print(f"  [XMP] {len(nombres)} nombres de capa encontrados vía atributos")
        return [{"name": n, "visible": True, "locked": False} for n in nombres]

    print("  [XMP] No se encontraron capas con estructura reconocible.")
    print("  Tip: usa debug_xmp() para ver el XML crudo.")
    return []


def _extraer_attr(attrs_str: str, nombres: list) -> str | None:
    """Busca el valor de cualquiera de los nombres de atributo dados."""
    for nombre in nombres:
        patron = re.compile(
            rf'(?:^|\s){re.escape(nombre)}\s*=\s*["\']([^"\']*)["\']'
        )
        m = patron.search(attrs_str)
        if m:
            return m.group(1)
    return None


def debug_xmp(pdf_path: str, max_chars: int = 3000):
    """Vuelca los primeros max_chars del XMP para inspeccionarlo manualmente."""
    pdf = Pdf.open(pdf_path)
    try:
        xmp = pdf.Root["/Metadata"].read_bytes().decode("utf-8", errors="replace")
        pdf.close()
        print(f"\n{'─'*60}")
        print("  XMP (primeros", max_chars, "chars)")
        print(f"{'─'*60}")
        print(xmp[:max_chars])
        if len(xmp) > max_chars:
            print(f"\n  ... ({len(xmp) - max_chars} chars más)")
    except (KeyError, AttributeError):
        pdf.close()
        print("  Sin /Metadata.")


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 2: Stream nativo .ai embebido
# ─────────────────────────────────────────────────────────────────────────────

# Marcadores de capa en el formato .ai / PostScript de Illustrator
# %AI5_BeginLayer
# 1 1 1 1 0 0 0 0 0 0 Lb    ← flags: visible locked printable ...
# (Nombre de la capa) Ln     ← nombre entre paréntesis
# ...contenido...
# LB                         ← end layer
# %AI5_EndLayer

RE_BEGIN_LAYER = re.compile(
    rb'%AI(?:5|8|9|10)?_BeginLayer\s*\n'
    rb'([\d ]+)Lb\s*\n'          # flags en línea anterior a nombre
    rb'\(([^)]*)\)\s*Ln',        # nombre entre paréntesis
    re.DOTALL
)

# Versión alternativa que Illustrator CS+ usa:
# %%Layer: 1 1 1 1
# (Nombre) Ln
RE_LAYER_ALT = re.compile(
    rb'%%Layer:\s*([\d ]+)\s*\n(?:.*?\n)?\(([^)]*)\)\s*Ln',
    re.DOTALL
)

# Marcador de sub-capa / grupo de capas
RE_BEGIN_SUBLAYER = re.compile(
    rb'%AI5_BeginGroup\s*\n'
    rb'\(([^)]*)\)\s*Ln',
    re.DOTALL
)


def _buscar_stream_ai(pdf: Pdf) -> bytes | None:
    """
    Localiza el stream .ai embebido en el PDF.
    Illustrator lo guarda de varias formas según versión:
      - Como EmbeddedFile en /Names/EmbeddedFiles
      - Como stream con /Subtype /application#2Fpostscript o similar
      - Como objeto con clave /AI o comentario %AI en el contenido
    """
    # Intento 1: EmbeddedFiles
    try:
        ef = pdf.Root["/Names"]["/EmbeddedFiles"]["/Names"]
        for i in range(0, len(ef), 2):
            nombre = str(ef[i])
            if nombre.endswith(".ai") or "illustrator" in nombre.lower():
                stream_ref = ef[i + 1]
                if "/EF" in stream_ref:
                    data = stream_ref["/EF"]["/F"].read_bytes()
                    print(f"  [AI stream] Encontrado en EmbeddedFiles: '{nombre}' "
                          f"({len(data):,} bytes)")
                    return data
    except (KeyError, AttributeError, IndexError, TypeError):
        pass

    # Intento 2: Recorrer todos los objetos buscando stream con contenido .ai
    print("  [AI stream] Buscando en objetos del PDF...")
    for obj in pdf.objects:
        try:
            if not hasattr(obj, "stream_dict"):
                continue
            # Verificar si parece un stream Illustrator
            subtype = str(obj.stream_dict.get("/Subtype", ""))
            if "postscript" in subtype.lower() or "illustrator" in subtype.lower():
                data = obj.read_bytes()
                if b"%AI" in data[:200] or b"%!PS-Adobe" in data[:200]:
                    print(f"  [AI stream] Encontrado por /Subtype ({len(data):,} bytes)")
                    return data
        except Exception:
            continue

    # Intento 3: Primer stream grande que contenga %AI5_BeginLayer
    for obj in pdf.objects:
        try:
            if not hasattr(obj, "read_bytes"):
                continue
            # Solo probar objetos con tamaño razonable (>10KB)
            length = obj.stream_dict.get("/Length", 0)
            if int(length) < 10_000:
                continue
            data = obj.read_bytes()
            if b"%AI5_BeginLayer" in data or b"%%Layer:" in data:
                print(f"  [AI stream] Encontrado por contenido ({len(data):,} bytes)")
                return data
        except Exception:
            continue

    return None


def leer_capas_stream_ai(pdf_path: str) -> list[dict]:
    """
    Extrae la jerarquía de capas desde el stream nativo .ai embebido.
    Devuelve lista de dicts con jerarquía real:
        [{
            name     : str,
            visible  : bool,
            locked   : bool,
            printable: bool,
            color    : int,       # color de etiqueta (0-26)
            children : [...]      # sub-capas
        }, ...]

    Solo funciona si el PDF fue guardado con
    "Preserve Illustrator Editing Capabilities" activado.
    """
    pdf  = Pdf.open(pdf_path)
    data = _buscar_stream_ai(pdf)
    pdf.close()

    if data is None:
        print("  [AI stream] No encontrado.")
        print("  El PDF debe guardarse con 'Preserve Illustrator Editing")
        print("  Capabilities' activado (Archivo > Guardar como > PDF > Avanzado).")
        return []

    # Descomprimir si es necesario
    if data[:2] in (b'\x78\x9c', b'\x78\xda', b'\x78\x01'):
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass

    capas = _parsear_capas_ai(data)
    return capas


def _parsear_capas_ai(data: bytes) -> list[dict]:
    """
    Parsea los marcadores de capa del stream .ai.
    Maneja anidamiento con un stack.
    """
    lineas = data.split(b"\n")
    stack  = [[]]   # stack de listas de capas; stack[0] = raíz
    i      = 0

    while i < len(lineas):
        linea = lineas[i].strip()

        # ── Inicio de capa ────────────────────────────────────────────────────
        if linea in (b"%AI5_BeginLayer", b"%%BeginSetup"):
            # Buscar flags (Lb) y nombre (Ln) en las siguientes líneas
            flags_line = b""
            nombre     = ""
            for j in range(i + 1, min(i + 10, len(lineas))):
                l = lineas[j].strip()
                if l.endswith(b"Lb"):
                    flags_line = l
                if l.endswith(b"Ln"):
                    m = re.match(rb'\(([^)]*)\)', l)
                    if m:
                        nombre = m.group(1).decode("latin-1", errors="replace")
                    break

            if nombre:
                flags  = flags_line.replace(b"Lb", b"").split()
                capa   = {
                    "name"     : nombre,
                    "visible"  : _flag(flags, 0, True),
                    "locked"   : not _flag(flags, 1, True),
                    "printable": _flag(flags, 2, True),
                    "color"    : int(flags[7]) if len(flags) > 7 else 0,
                    "children" : [],
                }
                stack[-1].append(capa)
                stack.append(capa["children"])

        # ── Fin de capa ───────────────────────────────────────────────────────
        elif linea in (b"LB", b"%AI5_EndLayer") and len(stack) > 1:
            stack.pop()

        # ── Capa en formato alternativo (%%Layer:) ────────────────────────────
        elif linea.startswith(b"%%Layer:"):
            partes = linea.split()
            nombre = ""
            # El nombre viene en la siguiente línea con Ln
            if i + 1 < len(lineas):
                siguiente = lineas[i + 1].strip()
                m = re.match(rb'\(([^)]*)\)\s*Ln', siguiente)
                if m:
                    nombre = m.group(1).decode("latin-1", errors="replace")
            if nombre:
                flags = partes[1:]
                capa  = {
                    "name"     : nombre,
                    "visible"  : _flag(flags, 0, True),
                    "locked"   : not _flag(flags, 1, True),
                    "printable": _flag(flags, 2, True),
                    "color"    : int(flags[3]) if len(flags) > 3 else 0,
                    "children" : [],
                }
                stack[-1].append(capa)

        i += 1

    return stack[0]


def _flag(flags: list, idx: int, default: bool) -> bool:
    """Lee un flag booleano de la lista; 1=True, 0=False."""
    try:
        return flags[idx] == b"1"
    except IndexError:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Imprimir árbol resultante
# ─────────────────────────────────────────────────────────────────────────────

COLORES_CAPA = {
    0: "rojo", 1: "naranja", 2: "amarillo", 3: "verde", 4: "azul",
    5: "violeta", 6: "magenta", 7: "marrón", 8: "negro",
}


def _imprimir_capa(capa: dict, prefijo: str = "", es_ultimo: bool = True):
    rama    = "└── " if es_ultimo else "├── "
    sangria = prefijo + ("    " if es_ultimo else "│   ")

    on      = capa.get("visible",   True)
    locked  = capa.get("locked",    False)
    hijos   = capa.get("children",  [])
    color   = COLORES_CAPA.get(capa.get("color", -1), "")

    icono  = "🟢" if on else "⚫"
    candado = " 🔒" if locked else ""
    tag    = f"  [{color}]" if color else ""
    carpeta = "📂" if hijos else "  "

    print(f"{prefijo}{rama}{carpeta} {icono} {capa['name']}{candado}{tag}")

    for i, hijo in enumerate(hijos):
        _imprimir_capa(hijo, sangria, es_ultimo=(i == len(hijos) - 1))


def ver_arbol_illustrator(pdf_path: str):
    """
    Imprime la jerarquía real de capas desde el stream .ai interno.
    Más fiel al panel de Capas de Illustrator que /Order.
    """
    print(f"\n{'═' * 60}")
    print(f"  Capas Illustrator (stream .ai): {pdf_path}")
    print(f"{'═' * 60}")

    capas = leer_capas_stream_ai(pdf_path)

    if not capas:
        print("\n  Intentando desde XMP...")
        capas_xmp = leer_capas_xmp(pdf_path)
        if capas_xmp:
            for i, c in enumerate(capas_xmp):
                es_ult = (i == len(capas_xmp) - 1)
                rama   = "└── " if es_ult else "├── "
                on     = c.get("visible", True)
                print(f"  {rama}{'🟢' if on else '⚫'} {c['name']}")
        return

    print()
    for i, capa in enumerate(capas):
        _imprimir_capa(capa, "  ", es_ultimo=(i == len(capas) - 1))
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    # Árbol desde stream .ai (más completo, requiere editing capabilities)
    ver_arbol_illustrator(PDF)

    # Solo XMP
    # leer_capas_xmp(PDF)

    # Ver XMP crudo si nada funciona
    # debug_xmp(PDF)

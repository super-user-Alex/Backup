"""
ocg_tree.py  (v2 — corregido para Illustrator)
===============================================
Illustrator escribe /Order con esta estructura:

    /Order [
      OCG_padre_ref          ← referencia suelta
      [                      ← array siguiente = hijos del anterior
        OCG_hijo_ref
        [                    ← idem para nietos
          OCG_nieto_ref
        ]
        OCG_hijo2_ref
      ]
      OCG_hoja_ref           ← sin array después → sin hijos
    ]

La versión anterior asumía que padre e hijos iban dentro del MISMO array.
Esta versión usa lookahead: cuando el elemento i es un OCG ref y el i+1
es un Array, ese array contiene los hijos.

Dependencias:
    pip install pikepdf
"""

import pikepdf
from pikepdf import Pdf


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _leer_estados(pdf: Pdf) -> dict:
    """Devuelve {objgen: bool} → True si el OCG está visible (ON)."""
    estados = {}
    try:
        oc_props = pdf.Root["/OCProperties"]
        off_set  = set()
        if "/OFF" in oc_props["/D"]:
            for r in oc_props["/D"]["/OFF"]:
                if hasattr(r, "objgen"):
                    off_set.add(r.objgen)
        for ref in oc_props["/OCGs"]:
            estados[ref.objgen] = ref.objgen not in off_set
    except (KeyError, AttributeError):
        pass
    return estados


def _es_ocg_ref(item) -> bool:
    """True si el item es una referencia indirecta a un diccionario OCG."""
    if not hasattr(item, "objgen"):
        return False
    try:
        t = str(item.get("/Type", ""))
        return t in ("/OCG", "/OCMD", "")
    except Exception:
        return False


def _nombre_ocg(ref) -> str:
    try:
        return str(ref["/Name"])
    except (KeyError, AttributeError):
        return "(sin nombre)"


# ─────────────────────────────────────────────────────────────────────────────
# Motor de impresión recursivo
# ─────────────────────────────────────────────────────────────────────────────

def _imprimir_nivel(elementos: list, estados: dict,
                    alias_por_nombre: dict,
                    prefijo: str = ""):
    """
    Recorre una lista de elementos de /Order con lookahead:
    si el elemento i es un OCG ref (o String) y el i+1 es un Array,
    ese array contiene los hijos.
    """
    i = 0
    while i < len(elementos):
        item = elementos[i]

        # ¿Tiene hijos? → el siguiente elemento es un Array
        hijos = None
        if (i + 1 < len(elementos) and
                isinstance(elementos[i + 1], pikepdf.Array)):
            hijos = list(elementos[i + 1])

        # ¿Es el último nodo visible a este nivel?
        siguiente_visible = i + (2 if hijos is not None else 1)
        es_ultimo = (siguiente_visible >= len(elementos))

        rama    = "└── " if es_ultimo else "├── "
        sangria = prefijo + ("    " if es_ultimo else "│   ")

        # ── Caso A: referencia a OCG ─────────────────────────────────────────
        if _es_ocg_ref(item):
            nombre  = _nombre_ocg(item)
            on      = estados.get(item.objgen, True)
            icono   = "🟢" if on else "⚫"
            aliases = alias_por_nombre.get(nombre, [])
            sufijo  = "  » " + ", ".join(aliases) if aliases else ""
            carpeta = "📂" if hijos else "  "
            print(f"{prefijo}{rama}{carpeta} {icono} {nombre}{sufijo}")
            if hijos:
                _imprimir_nivel(hijos, estados, alias_por_nombre, sangria)
                i += 2
                continue

        # ── Caso B: String → etiqueta de grupo ───────────────────────────────
        elif isinstance(item, pikepdf.String):
            etiqueta = str(item)
            print(f"{prefijo}{rama}📁 {etiqueta}")
            if hijos:
                _imprimir_nivel(hijos, estados, alias_por_nombre, sangria)
                i += 2
                continue

        # ── Caso C: Array suelto (grupo anónimo sin padre explícito) ─────────
        elif isinstance(item, pikepdf.Array):
            print(f"{prefijo}{rama}📁 (grupo anónimo)")
            _imprimir_nivel(list(item), estados, alias_por_nombre, sangria)

        i += 1


# ─────────────────────────────────────────────────────────────────────────────
# Función pública
# ─────────────────────────────────────────────────────────────────────────────

def ver_arbol_ocg(pdf_path: str, mostrar_alias: bool = False):
    """
    Imprime el árbol completo de OCGs.

    Args:
        mostrar_alias : Si True, muestra los alias /MC0, /MC1… junto a
                        cada OCG (requiere mover_pdf_directo.py en el path).
    Leyenda:
        🟢  visible (ON)      ⚫  oculto (OFF)
        📂  tiene sub-capas   📁  grupo/carpeta sin OCG propio
        »   alias en stream   (/MC0, /MC1…)
    """
    pdf = Pdf.open(pdf_path)

    try:
        oc_props = pdf.Root["/OCProperties"]
    except (KeyError, AttributeError):
        print("  Este PDF no tiene OCGs.")
        pdf.close()
        return

    estados = _leer_estados(pdf)

    # Alias opcionales
    alias_por_nombre: dict = {}
    if mostrar_alias:
        try:
            from mover_pdf_directo import _alias_a_nombre_ocg, _ocg_objgen_a_nombre
            ogmap = _ocg_objgen_a_nombre(pdf)
            page  = pdf.pages[0]
            for alias, nombre in _alias_a_nombre_ocg(page.obj, ogmap).items():
                alias_por_nombre.setdefault(nombre, []).append("/" + alias)
        except ImportError:
            print("  (mover_pdf_directo.py no encontrado — alias omitidos)\n")

    total = len(list(oc_props.get("/OCGs", [])))
    print(f"\n{'═' * 60}")
    print(f"  Árbol OCG: {pdf_path}   ({total} OCGs)")
    print(f"{'═' * 60}")

    try:
        order = oc_props["/D"]["/Order"]
    except (KeyError, AttributeError):
        order = None

    if not order or len(list(order)) == 0:
        print("  (sin /Order — listado plano)\n")
        ocgs = list(oc_props.get("/OCGs", []))
        for i, ref in enumerate(ocgs):
            rama   = "└── " if i == len(ocgs) - 1 else "├── "
            nombre = _nombre_ocg(ref)
            on     = estados.get(ref.objgen, True)
            print(f"  {rama}{'🟢' if on else '⚫'} {nombre}")
    else:
        _imprimir_nivel(list(order), estados, alias_por_nombre, prefijo="  ")

    # ── OCGs huérfanos ────────────────────────────────────────────────────────
    try:
        en_order: set = set()
        _recopilar_objgens(order, en_order)
        todos     = {ref.objgen for ref in oc_props["/OCGs"]}
        huerfanos = todos - en_order
        if huerfanos:
            print(f"\n  {'─' * 50}")
            print(f"  ⚠️  OCGs fuera de /Order ({len(huerfanos)}):")
            for ref in oc_props["/OCGs"]:
                if ref.objgen in huerfanos:
                    nombre = _nombre_ocg(ref)
                    on     = estados.get(ref.objgen, True)
                    print(f"    • {'🟢' if on else '⚫'} {nombre}")
    except Exception:
        pass

    print()
    pdf.close()


def _recopilar_objgens(item, conjunto: set):
    if item is None:
        return
    if isinstance(item, pikepdf.Array):
        for sub in item:
            _recopilar_objgens(sub, conjunto)
    elif hasattr(item, "objgen"):
        try:
            _ = item["/Name"]
            conjunto.add(item.objgen)
        except (KeyError, AttributeError):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Debug: volcar /Order en crudo
# ─────────────────────────────────────────────────────────────────────────────

def debug_order_raw(pdf_path: str):
    """
    Vuelca /Order tal cual, indentado, para ver la estructura real del PDF.
    Úsala si el árbol no sale bien.
    """
    pdf = Pdf.open(pdf_path)
    try:
        order = pdf.Root["/OCProperties"]["/D"]["/Order"]
    except (KeyError, AttributeError):
        print("  Sin /Order.")
        pdf.close()
        return

    print(f"\n{'─' * 55}")
    print("  /Order RAW")
    print(f"{'─' * 55}")
    _volcar(order, "  ")
    print()
    pdf.close()


def _volcar(item, ind: str = ""):
    if isinstance(item, pikepdf.Array):
        print(f"{ind}[")
        for sub in item:
            _volcar(sub, ind + "  ")
        print(f"{ind}]")
    elif hasattr(item, "objgen"):
        try:
            nombre = str(item["/Name"])
            print(f"{ind}OCG ref → '{nombre}'  {item.objgen}")
        except Exception:
            print(f"{ind}ref → {item.objgen}")
    elif isinstance(item, pikepdf.String):
        print(f"{ind}String → '{str(item)}'")
    else:
        print(f"{ind}{type(item).__name__} → {item}")


# ─────────────────────────────────────────────────────────────────────────────
# Ejemplo de uso
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    # Árbol limpio
    ver_arbol_ocg(PDF)

    # Con alias /MC0, /MC1… (requiere mover_pdf_directo.py)
    # ver_arbol_ocg(PDF, mostrar_alias=True)

    # Si el árbol aún no sale bien → ver estructura cruda
    # debug_order_raw(PDF)

"""
ocg_tree.py
===========
Muestra el árbol completo de OCGs (Optional Content Groups) de un PDF,
tal como lo define /OCProperties/D/Order en el catálogo.

Illustrator y otros programas usan esta estructura para representar
la jerarquía de capas (grupos dentro de grupos).

Dependencias:
    pip install pikepdf
"""

import pikepdf
from pikepdf import Pdf


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


def _leer_intent(ref) -> str:
    """Devuelve el Intent del OCG: View / Design / All (o vacío)."""
    try:
        intent = ref["/Intent"]
        if isinstance(intent, pikepdf.Array):
            return ",".join(str(i) for i in intent).replace("/", "")
        return str(intent).lstrip("/")
    except (KeyError, AttributeError):
        return ""


def _leer_tipo(ref) -> str:
    """Devuelve el Type del OCG si existe (/OCG o /OCMD)."""
    try:
        return str(ref["/Type"]).lstrip("/")
    except (KeyError, AttributeError):
        return "OCG"


def _imprimir_nodo(item, estados: dict, prefijo: str = "", es_ultimo: bool = True):
    """
    Imprime recursivamente un nodo del árbol /Order.

    Un nodo puede ser:
      - Una referencia indirecta a un OCG  → hoja
      - Un Array                           → grupo (primer elemento puede ser
                                             un OCG padre, resto son hijos)
      - Un String PDF                      → etiqueta de grupo sin OCG asociado
    """
    rama   = "└── " if es_ultimo else "├── "
    sangria = prefijo + ("    " if es_ultimo else "│   ")

    # ── Caso 1: Array → grupo jerárquico ────────────────────────────────────
    if isinstance(item, pikepdf.Array):
        elementos = list(item)
        if not elementos:
            return

        primer = elementos[0]

        # El primer elemento puede ser un OCG (padre del grupo) o un String
        if isinstance(primer, pikepdf.String):
            # Etiqueta de grupo sin OCG directo
            etiqueta = str(primer)
            print(f"{prefijo}{rama}📁 [{etiqueta}]")
            hijos = elementos[1:]
        else:
            # El primer elemento es el OCG padre
            try:
                nombre = str(primer["/Name"])
                on     = estados.get(primer.objgen, True)
                intent = _leer_intent(primer)
                tipo   = _leer_tipo(primer)
                icono  = "🟢" if on else "⚫"
                extra  = f"  ({intent})" if intent else ""
                print(f"{prefijo}{rama}{icono} {nombre}{extra}  [{tipo}]")
            except Exception:
                print(f"{prefijo}{rama}❓ (nodo no resoluble)")
            hijos = elementos[1:]

        for j, hijo in enumerate(hijos):
            _imprimir_nodo(hijo, estados, sangria, es_ultimo=(j == len(hijos) - 1))

    # ── Caso 2: Referencia indirecta a un OCG ────────────────────────────────
    elif hasattr(item, "objgen"):
        try:
            nombre = str(item["/Name"])
            on     = estados.get(item.objgen, True)
            intent = _leer_intent(item)
            tipo   = _leer_tipo(item)
            icono  = "🟢" if on else "⚫"
            extra  = f"  ({intent})" if intent else ""
            print(f"{prefijo}{rama}{icono} {nombre}{extra}  [{tipo}]")
        except Exception:
            print(f"{prefijo}{rama}❓ (referencia no resoluble)")

    # ── Caso 3: String → etiqueta suelta ─────────────────────────────────────
    elif isinstance(item, pikepdf.String):
        print(f"{prefijo}{rama}📁 [{str(item)}]")


def ver_arbol_ocg(pdf_path: str):
    """
    Imprime el árbol completo de OCGs de un PDF.

    La jerarquía refleja /OCProperties/D/Order del catálogo,
    que es exactamente lo que muestra Illustrator en el panel de capas.

    Leyenda
    -------
    🟢  OCG visible (ON)
    ⚫  OCG oculto  (OFF)
    📁  Grupo / carpeta sin OCG propio
    [View] / [Design] / [All]  → Intent del OCG
    """
    pdf = Pdf.open(pdf_path)

    try:
        oc_props = pdf.Root["/OCProperties"]
    except (KeyError, AttributeError):
        print("  Este PDF no tiene OCGs.")
        pdf.close()
        return

    estados = _leer_estados(pdf)

    # Total de OCGs registrados en /OCGs
    try:
        total = len(list(oc_props["/OCGs"]))
    except (KeyError, TypeError):
        total = 0

    print(f"\n{'═' * 55}")
    print(f"  Árbol OCG: {pdf_path}   ({total} OCGs en catálogo)")
    print(f"{'═' * 55}")

    # /Order puede no existir (PDF sin estructura jerárquica)
    try:
        order = oc_props["/D"]["/Order"]
    except (KeyError, AttributeError):
        order = None

    if order is None or len(list(order)) == 0:
        # Sin /Order → listar plano desde /OCGs
        print("  (sin /Order definido — listado plano)\n")
        try:
            ocgs = list(oc_props["/OCGs"])
            for i, ref in enumerate(ocgs):
                es_ult = (i == len(ocgs) - 1)
                rama   = "└── " if es_ult else "├── "
                nombre = str(ref["/Name"])
                on     = estados.get(ref.objgen, True)
                icono  = "🟢" if on else "⚫"
                print(f"  {rama}{icono} {nombre}")
        except Exception as e:
            print(f"  Error leyendo /OCGs: {e}")
    else:
        nodos = list(order)
        for i, nodo in enumerate(nodos):
            _imprimir_nodo(nodo, estados, prefijo="  ", es_ultimo=(i == len(nodos) - 1))

    # ── OCGs huérfanos (en /OCGs pero ausentes en /Order) ────────────────────
    try:
        en_order  = set()
        _recopilar_objgens(order, en_order)
        todos     = {ref.objgen for ref in oc_props["/OCGs"]}
        huerfanos = todos - en_order
        if huerfanos:
            print(f"\n  {'─' * 50}")
            print(f"  ⚠️  OCGs en catálogo pero FUERA de /Order ({len(huerfanos)}):")
            for ref in oc_props["/OCGs"]:
                if ref.objgen in huerfanos:
                    nombre = str(ref["/Name"])
                    on     = estados.get(ref.objgen, True)
                    icono  = "🟢" if on else "⚫"
                    print(f"    • {icono} {nombre}")
    except Exception:
        pass

    print()
    pdf.close()


def _recopilar_objgens(item, conjunto: set):
    """Recorre /Order y acumula los objgen de todos los OCGs referenciados."""
    if item is None:
        return
    if isinstance(item, pikepdf.Array):
        for sub in item:
            _recopilar_objgens(sub, conjunto)
    elif hasattr(item, "objgen"):
        try:
            _ = item["/Name"]   # confirma que es un OCG
            conjunto.add(item.objgen)
        except (KeyError, AttributeError):
            pass


# ─────────────────────────────────────────────────────────────
# EXTRA: ver también los alias de página (MC0, MC1…) junto al árbol
# ─────────────────────────────────────────────────────────────

def ver_arbol_ocg_con_alias(pdf_path: str):
    """
    Como ver_arbol_ocg() pero añade al lado de cada OCG
    los alias (/MC0, /MC1…) que usa en el content stream de la página 1.
    """
    from mover_pdf_directo import _alias_a_nombre_ocg, _ocg_objgen_a_nombre

    pdf    = Pdf.open(pdf_path)
    ogmap  = _ocg_objgen_a_nombre(pdf)
    page   = pdf.pages[0]

    # Invertir: nombre_ocg → [alias1, alias2, ...]
    alias_por_nombre: dict[str, list] = {}
    for alias, nombre in _alias_a_nombre_ocg(page.obj, ogmap).items():
        alias_por_nombre.setdefault(nombre, []).append("/" + alias)

    pdf.close()

    # Monkey-patch temporal de _imprimir_nodo para añadir los alias
    # (más sencillo: reimprimir el árbol con la info extra)
    pdf2    = Pdf.open(pdf_path)
    estados = _leer_estados(pdf2)

    try:
        oc_props = pdf2.Root["/OCProperties"]
        order    = oc_props["/D"]["/Order"]
    except (KeyError, AttributeError):
        pdf2.close()
        print("  Sin /Order. Usa ver_arbol_ocg() directamente.")
        return

    total = len(list(oc_props["/OCGs"]))
    print(f"\n{'═' * 65}")
    print(f"  Árbol OCG + alias de página 1: {pdf_path}   ({total} OCGs)")
    print(f"{'═' * 65}")

    def _nodo_con_alias(item, prefijo="", es_ultimo=True):
        rama    = "└── " if es_ultimo else "├── "
        sangria = prefijo + ("    " if es_ultimo else "│   ")

        if isinstance(item, pikepdf.Array):
            elementos = list(item)
            if not elementos:
                return
            primer = elementos[0]
            if isinstance(primer, pikepdf.String):
                print(f"{prefijo}{rama}📁 [{str(primer)}]")
                hijos = elementos[1:]
            else:
                try:
                    nombre  = str(primer["/Name"])
                    on      = estados.get(primer.objgen, True)
                    icono   = "🟢" if on else "⚫"
                    aliases = alias_por_nombre.get(nombre, [])
                    sufijo  = "  alias: " + ", ".join(aliases) if aliases else ""
                    print(f"{prefijo}{rama}{icono} {nombre}{sufijo}")
                except Exception:
                    print(f"{prefijo}{rama}❓")
                hijos = elementos[1:]
            for j, hijo in enumerate(hijos):
                _nodo_con_alias(hijo, sangria, es_ultimo=(j == len(hijos) - 1))

        elif hasattr(item, "objgen"):
            try:
                nombre  = str(item["/Name"])
                on      = estados.get(item.objgen, True)
                icono   = "🟢" if on else "⚫"
                aliases = alias_por_nombre.get(nombre, [])
                sufijo  = "  alias: " + ", ".join(aliases) if aliases else ""
                print(f"{prefijo}{rama}{icono} {nombre}{sufijo}")
            except Exception:
                print(f"{prefijo}{rama}❓")

        elif isinstance(item, pikepdf.String):
            print(f"{prefijo}{rama}📁 [{str(item)}]")

    nodos = list(order)
    for i, nodo in enumerate(nodos):
        _nodo_con_alias(nodo, prefijo="  ", es_ultimo=(i == len(nodos) - 1))

    print()
    pdf2.close()


# ─────────────────────────────────────────────────────────────
# EJEMPLO DE USO
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PDF = "diseño.pdf"

    # Árbol básico
    ver_arbol_ocg(PDF)

    # Árbol + alias del content stream (requiere mover_pdf_directo.py)
    # ver_arbol_ocg_con_alias(PDF)

"""
ocg_remove.py
=============
Elimina físicamente todo el contenido de capas OCG (Optional Content Groups)
de un PDF, conservando únicamente la capa especificada.

Opera a bajo nivel: parsea los content streams página por página,
extrae los bloques BDC/EMC de OCG y reescribe los streams sin ellos.
También limpia el catálogo /OCProperties para que el PDF resultante
no tenga rastro de las capas eliminadas.

Uso:
    python ocg_remove.py input.pdf              # lista las capas disponibles
    python ocg_remove.py input.pdf "NombreCapa" # conserva solo esa capa
    python ocg_remove.py input.pdf "NombreCapa" -o salida.pdf
"""

import sys
import argparse
from typing import Optional

import pikepdf
from pikepdf import Array, Dictionary, Name, String, Pdf


# ──────────────────────────────────────────────────────────────────────────────
# 1.  Helpers para el catálogo de OCGs
# ──────────────────────────────────────────────────────────────────────────────

def get_ocgs(pdf: Pdf) -> dict[str, pikepdf.Object]:
    """
    Devuelve {nombre: objeto_OCG} para todas las capas del PDF.
    """
    ocgs = {}
    root = pdf.Root
    if "/OCProperties" not in root:
        return ocgs

    oc_props = root["/OCProperties"]
    if "/OCGs" not in oc_props:
        return ocgs

    for ocg_ref in oc_props["/OCGs"]:
        ocg = ocg_ref  # ya resuelto por pikepdf
        if "/Name" in ocg:
            name = str(ocg["/Name"])
            ocgs[name] = ocg

    return ocgs


def resolve_ocg_names(pdf: Pdf) -> dict[str, str]:
    """
    Crea un mapa {indirect_objid_str: nombre_capa} para búsquedas rápidas
    desde los operandos de contenido.
    """
    result = {}
    root = pdf.Root
    if "/OCProperties" not in root:
        return result

    oc_props = root["/OCProperties"]
    if "/OCGs" not in oc_props:
        return result

    for ocg_ref in oc_props["/OCGs"]:
        objid = str(ocg_ref.objgen)           # e.g. "(5, 0)"
        name  = str(ocg_ref["/Name"]) if "/Name" in ocg_ref else "?"
        result[objid] = name

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 2.  Parser de content stream a bajo nivel
# ──────────────────────────────────────────────────────────────────────────────

def filter_content_stream(
    page: pikepdf.Object,
    keep_layer: str,
    ocg_name_map: dict[str, str],
) -> bytes:
    """
    Recorre el content stream de la página token a token.
    Elimina físicamente los bloques BDC/EMC cuya capa no sea `keep_layer`.
    El contenido sin capa (base, no marcado) se conserva siempre.

    Estrategia de pila:
      - Al encontrar un 'BDC' con propiedad /OC se empuja a la pila:
          · True  → pertenece a la capa que queremos conservar
          · False → pertenece a otra capa (se descarta)
          · None  → BDC genérico sin /OC (se conserva)
      - Al encontrar 'EMC' se hace pop.
      - Se emiten tokens solo cuando todos los niveles activos son True/None.
    """
    instructions = []
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception as e:
        print(f"  [!] No se pudo parsear el content stream: {e}", file=sys.stderr)
        return page.read_raw_bytes() if hasattr(page, "read_raw_bytes") else b""

    output_instructions = []
    # pila: cada entrada es True (conservar) | False (descartar) | None (neutro)
    stack: list[Optional[bool]] = []

    def _is_active() -> bool:
        """True si todos los niveles activos permiten emitir contenido."""
        return all(v is not False for v in stack)

    for operands, operator in instructions:
        op = str(operator)

        if op == "BDC":
            # BDC puede tener: /Tag  /Properties  BDC
            # o bien:          /Tag  <<dict>>  BDC
            # El segundo operando indica la propiedad/OCG.
            keep: Optional[bool] = None   # neutro por defecto

            if len(operands) >= 2:
                tag = operands[0]
                prop = operands[1]

                # El tag debe ser /OC para que sea un marcado de capa
                if str(tag) == "/OC":
                    ocg_obj = None

                    # Caso 1: referencia indirecta (Name en Resources/Properties)
                    if isinstance(prop, pikepdf.Name):
                        prop_name = str(prop)
                        # Resolver desde Resources/Properties de la página
                        try:
                            ocg_obj = page["/Resources"]["/Properties"][prop_name]
                        except (KeyError, TypeError):
                            pass

                    # Caso 2: diccionario inline
                    elif isinstance(prop, pikepdf.Dictionary):
                        ocg_obj = prop

                    if ocg_obj is not None:
                        objid = str(ocg_obj.objgen)
                        layer_name = ocg_name_map.get(objid)
                        if layer_name is not None:
                            keep = (layer_name == keep_layer)
                        else:
                            # Intentar por /Name si está embebido
                            if "/Name" in ocg_obj:
                                lname = str(ocg_obj["/Name"])
                                keep = (lname == keep_layer)

            stack.append(keep)

            if _is_active():
                output_instructions.append((operands, operator))

        elif op == "EMC":
            # Emitir el EMC solo si estábamos activos ANTES del pop
            if _is_active():
                output_instructions.append((operands, operator))
            if stack:
                stack.pop()

        else:
            if _is_active():
                output_instructions.append((operands, operator))

    return pikepdf.unparse_content_stream(output_instructions)


# ──────────────────────────────────────────────────────────────────────────────
# 3.  Limpieza del catálogo /OCProperties
# ──────────────────────────────────────────────────────────────────────────────

def clean_oc_catalog(pdf: Pdf, keep_layer: str) -> None:
    """
    Actualiza /OCProperties para que solo refleje la capa conservada.
    Elimina referencias a las demás capas en /OCGs, /D (default config),
    /Configs, y cualquier entrada /Order o /ON.
    """
    root = pdf.Root
    if "/OCProperties" not in root:
        return

    oc_props = root["/OCProperties"]
    if "/OCGs" not in oc_props:
        return

    # Identificar los OCGs que queremos conservar vs. eliminar
    keep_refs  = []
    del_objids = set()

    for ocg_ref in oc_props["/OCGs"]:
        name = str(ocg_ref["/Name"]) if "/Name" in ocg_ref else ""
        if name == keep_layer:
            keep_refs.append(ocg_ref)
        else:
            del_objids.add(ocg_ref.objgen)

    # Reemplazar /OCGs
    oc_props["/OCGs"] = Array(keep_refs)

    # Limpiar /D (configuración por defecto)
    if "/D" in oc_props:
        d = oc_props["/D"]

        for key in ("/ON", "/OFF", "/Order", "/AS"):
            if key in d:
                if key in ("/ON", "/OFF"):
                    d[key] = Array([r for r in d[key]
                                    if r.objgen not in del_objids])
                elif key == "/Order":
                    d[key] = _filter_order(d[key], del_objids)
                else:
                    del d[key]

        # Establecer el estado de la capa conservada como ON
        if keep_refs:
            d["/ON"]  = Array(keep_refs)
            d["/OFF"] = Array([])

    # Si hay /Configs, limpiarlos también
    if "/Configs" in oc_props:
        del oc_props["/Configs"]


def _filter_order(order_array: pikepdf.Array, del_objids: set) -> pikepdf.Array:
    """Filtra recursivamente el array /Order eliminando OCGs no deseados."""
    result = []
    for item in order_array:
        if isinstance(item, pikepdf.Array):
            filtered = _filter_order(item, del_objids)
            if filtered:
                result.append(filtered)
        else:
            if item.objgen not in del_objids:
                result.append(item)
    return Array(result)


# ──────────────────────────────────────────────────────────────────────────────
# 4.  Eliminar referencias /OC en anotaciones y XObjects
# ──────────────────────────────────────────────────────────────────────────────

def remove_oc_from_annots(pdf: Pdf, del_objids: set) -> None:
    """
    Elimina o filtra anotaciones y XObjects que referenciaban las capas borradas.
    """
    for page in pdf.pages:
        # Anotaciones
        if "/Annots" in page:
            kept_annots = []
            for annot in page["/Annots"]:
                if "/OC" in annot:
                    if annot["/OC"].objgen in del_objids:
                        continue   # eliminar esta anotación
                kept_annots.append(annot)
            page["/Annots"] = Array(kept_annots)

        # XObjects en Resources
        try:
            xobjects = page["/Resources"]["/XObject"]
            for key in list(xobjects.keys()):
                xobj = xobjects[key]
                if "/OC" in xobj:
                    if xobj["/OC"].objgen in del_objids:
                        del xobjects[key]
        except (KeyError, TypeError):
            pass


# ──────────────────────────────────────────────────────────────────────────────
# 5.  Proceso principal
# ──────────────────────────────────────────────────────────────────────────────

def list_layers(input_path: str) -> None:
    with Pdf.open(input_path) as pdf:
        ocgs = get_ocgs(pdf)
        if not ocgs:
            print("Este PDF no tiene capas OCG.")
            return
        print(f"\nCapas OCG encontradas en '{input_path}':")
        for i, name in enumerate(ocgs, 1):
            print(f"  {i}. {name!r}")
        print()


def remove_other_layers(
    input_path: str,
    keep_layer: str,
    output_path: str,
) -> None:
    print(f"\n[+] Abriendo: {input_path}")
    with Pdf.open(input_path) as pdf:
        ocgs = get_ocgs(pdf)

        if not ocgs:
            print("El PDF no tiene capas OCG. Nada que hacer.")
            return

        if keep_layer not in ocgs:
            print(f"[!] La capa '{keep_layer}' no existe en el PDF.")
            print("    Capas disponibles:", list(ocgs.keys()))
            sys.exit(1)

        print(f"[+] Capa a conservar: {keep_layer!r}")
        print(f"[+] Capas a eliminar: {[n for n in ocgs if n != keep_layer]}")

        # Mapa objid → nombre de capa (para resolver referencias en streams)
        ocg_name_map = resolve_ocg_names(pdf)

        # ── Procesar cada página ──────────────────────────────────────────────
        for page_num, page in enumerate(pdf.pages, 1):
            print(f"  Página {page_num}: procesando content stream…")
            new_stream = filter_content_stream(page, keep_layer, ocg_name_map)

            # Reemplazar el stream de la página
            # Una página puede tener un stream único o un array de streams
            if "/Contents" in page:
                contents = page["/Contents"]
                if isinstance(contents, pikepdf.Array):
                    # Consolidar en un único stream
                    combined = b""
                    for s in contents:
                        combined += bytes(s.read_raw_bytes()) + b"\n"
                    # Re-filtrar el stream combinado (ya hecho arriba de forma individual)
                    # Nota: filter_content_stream trabaja sobre la página completa,
                    # pikepdf ya resuelve el array internamente en parse_content_stream
                    new_stream = filter_content_stream(page, keep_layer, ocg_name_map)

                # Crear un nuevo stream object
                new_content = pdf.make_stream(new_stream)
                page["/Contents"] = new_content

        # ── Identificar objids a eliminar para limpiar anotaciones ───────────
        del_objids = set()
        for name, ocg in ocgs.items():
            if name != keep_layer:
                del_objids.add(ocg.objgen)

        # ── Limpiar catálogo OCProperties ─────────────────────────────────────
        print("[+] Limpiando catálogo /OCProperties…")
        clean_oc_catalog(pdf, keep_layer)

        # ── Limpiar anotaciones y XObjects ────────────────────────────────────
        print("[+] Limpiando anotaciones y XObjects con referencias a capas eliminadas…")
        remove_oc_from_annots(pdf, del_objids)

        # ── Guardar ───────────────────────────────────────────────────────────
        print(f"[+] Guardando en: {output_path}")
        pdf.save(output_path, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        print("[✓] Hecho.\n")


# ──────────────────────────────────────────────────────────────────────────────
# 6.  CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Elimina físicamente capas OCG de un PDF conservando solo la indicada."
    )
    parser.add_argument("input", help="PDF de entrada")
    parser.add_argument(
        "layer",
        nargs="?",
        default=None,
        help="Nombre de la capa OCG a CONSERVAR (si se omite, lista las capas)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="PDF de salida (por defecto: input_ocg_<capa>.pdf)",
    )

    args = parser.parse_args()

    if args.layer is None:
        list_layers(args.input)
        return

    output = args.output or args.input.replace(".pdf", f"_ocg_{args.layer}.pdf")
    remove_other_layers(args.input, args.layer, output)


if __name__ == "__main__":
    main()

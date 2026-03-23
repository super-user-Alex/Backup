import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# -----------------------------
# GENERAR CLAVE DESDE PASSWORD
# -----------------------------
def generar_clave(password: str, salt: bytes):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

# -----------------------------
# CIFRAR UN ARCHIVO
# -----------------------------
def cifrar_archivo(ruta_entrada, ruta_salida, password):
    salt = os.urandom(16)
    clave = generar_clave(password, salt)
    f = Fernet(clave)

    with open(ruta_entrada, "rb") as file:
        datos = file.read()

    cifrado = f.encrypt(datos)

    with open(ruta_salida, "wb") as file:
        file.write(salt + cifrado)

# -----------------------------
# RECORRER CARPETA
# -----------------------------
def cifrar_carpeta(carpeta_entrada, carpeta_salida, password):
    for root, dirs, files in os.walk(carpeta_entrada):
        for file in files:
            ruta_completa = os.path.join(root, file)

            # Mantener estructura de carpetas
            ruta_relativa = os.path.relpath(ruta_completa, carpeta_entrada)
            salida_completa = os.path.join(carpeta_salida, ruta_relativa + ".enc")

            # Crear carpeta destino si no existe
            os.makedirs(os.path.dirname(salida_completa), exist_ok=True)

            print(f"Cifrando: {ruta_completa}")
            cifrar_archivo(ruta_completa, salida_completa, password)

def descifrar_archivo(ruta_entrada, ruta_salida, password):
    with open(ruta_entrada, "rb") as file:
        contenido = file.read()

    salt = contenido[:16]
    cifrado = contenido[16:]

    clave = generar_clave(password, salt)
    f = Fernet(clave)

    datos = f.decrypt(cifrado)

    with open(ruta_salida, "wb") as file:
        file.write(datos)


def descifrar_carpeta(carpeta_entrada, carpeta_salida, password):
    for root, dirs, files in os.walk(carpeta_entrada):
        for file in files:
            ruta_completa = os.path.join(root, file)

            # Quitar extensión .enc
            ruta_relativa = os.path.relpath(ruta_completa, carpeta_entrada)
            ruta_relativa = ruta_relativa.replace(".enc", "")

            salida_completa = os.path.join(carpeta_salida, ruta_relativa)

            os.makedirs(os.path.dirname(salida_completa), exist_ok=True)

            print(f"Descifrando: {ruta_completa}")
            descifrar_archivo(ruta_completa, salida_completa, password)

# -----------------------------
# USO
# -----------------------------
if __name__ == "__main__":
    carpeta_entrada = "to_encrip"
    carpeta_salida = "My_data"
    password = ""

    # cifrar_carpeta(carpeta_entrada, carpeta_salida, password)

    descifrar_carpeta(carpeta_salida, 'des', password)

    print("Carpeta encriptada correctamente")

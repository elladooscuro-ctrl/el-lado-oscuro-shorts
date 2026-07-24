"""
El Lado Oscuro - Generador automatico de YouTube Shorts
=========================================================
Flujo:
1. Buscar en Drive la imagen mas antigua dentro de la carpeta "Pendientes/"
2. Extraer la frase desde el nombre del archivo (guiones -> espacios)
3. Generar narracion con edge-tts (voz es-MX-JorgeNeural), con 1.5s de
   silencio antes y despues de la frase
4. Generar un CTA final: pantalla negra + texto + voz diciendo
   "Sigueme si quieres mas verdades como esta."
5. Armar el video final (imagen fija + audio) con moviepy
6. Generar titulo y hashtags (Gemini, con fallback local sin IA)
7. Subir el video a YouTube (publico)
8. Guardar copia en Drive: "Videos Generados/YYYY-MM-DD/"
9. Mover la imagen origen a "Publicadas/" en Drive

Lecciones aplicadas del proyecto anterior (reels-automatizados):
- moviepy fijado en 1.0.3 (versiones nuevas rompen la API usada aqui)
- Pillow: shim de Image.ANTIALIAS para compatibilidad con moviepy 1.0.3
- OAuth de Drive (no service account: sin cuota de almacenamiento propio)
- Workflow con cron cada 15 min + mecanismo de catch-up (ver .github/workflows)
- Evitar DeepSeek para controlar costos; fallback 100% local sin IA
- Un solo job de publicacion por corrida (bloque concurrency en el workflow)
"""

import os
import re
import json
import random
import datetime
import tempfile
import subprocess
from pathlib import Path

import edge_tts
import asyncio

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ---------------------------------------------------------------------------
# Shim de compatibilidad Pillow / moviepy 1.0.3
# ---------------------------------------------------------------------------
from PIL import Image
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeAudioClip,
    concatenate_videoclips,
    concatenate_audioclips,
    TextClip,
    CompositeVideoClip,
    ColorClip,
    afx,
)

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

VOZ = "es-MX-JorgeNeural"
SILENCIO_INICIO = 1.5  # segundos
SILENCIO_FIN = 1.5     # segundos
TEXTO_CTA = "Sigueme si quieres mas verdades como esta."

CARPETA_PENDIENTES = "Pendientes"
CARPETA_PUBLICADAS = "Publicadas"
CARPETA_VIDEOS_GENERADOS = "Videos Generados"

DRIVE_FOLDER_ID_RAIZ = os.environ.get("DRIVE_FOLDER_ID_RAIZ")  # carpeta raiz del proyecto en Drive
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ANCHO, ALTO = 1080, 1920  # formato vertical short

TMP_DIR = Path(tempfile.mkdtemp(prefix="lado_oscuro_"))


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

def frase_desde_nombre_archivo(nombre_archivo: str) -> str:
    """
    Convierte 'la-verdad-duele-pero-libera.jpg' -> 'la verdad duele pero libera'
    Quita extension, reemplaza guiones por espacios, capitaliza la primera letra.
    """
    nombre = Path(nombre_archivo).stem
    frase = nombre.replace("-", " ").replace("_", " ").strip()
    frase = re.sub(r"\s+", " ", frase)
    if frase:
        frase = frase[0].upper() + frase[1:]
    return frase


async def generar_audio_tts(texto: str, ruta_salida: Path, voz: str = VOZ):
    comunicador = edge_tts.Communicate(texto, voz)
    await comunicador.save(str(ruta_salida))


def agregar_silencios(ruta_audio_in: Path, ruta_audio_out: Path,
                       silencio_inicio: float, silencio_fin: float):
    """Usa moviepy para anteponer/agregar silencio a un audio."""
    clip = AudioFileClip(str(ruta_audio_in))
    silencio_i_path = TMP_DIR / "sil_i.mp3"
    silencio_f_path = TMP_DIR / "sil_f.mp3"
    _generar_silencio_mp3(silencio_i_path, silencio_inicio)
    _generar_silencio_mp3(silencio_f_path, silencio_fin)

    partes = [AudioFileClip(str(silencio_i_path)), clip, AudioFileClip(str(silencio_f_path))]
    audio_final = concatenate_audioclips(partes)
    audio_final.write_audiofile(str(ruta_audio_out), fps=44100, logger=None)
    for p in partes:
        p.close()
    audio_final.close()


def _generar_silencio_mp3(ruta_salida: Path, duracion: float):
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"anullsrc=r=44100:cl=stereo", "-t", str(duracion),
        "-q:a", "9", str(ruta_salida)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def crear_clip_imagen_fija(ruta_imagen: Path, duracion: float) -> ImageClip:
    clip = ImageClip(str(ruta_imagen)).set_duration(duracion)
    clip = clip.resize(height=ALTO)
    if clip.w < ANCHO:
        clip = clip.resize(width=ANCHO)
    clip = clip.crop(
        x_center=clip.w / 2, y_center=clip.h / 2, width=ANCHO, height=ALTO
    )
    return clip


def crear_clip_cta(duracion: float) -> CompositeVideoClip:
    """Pantalla negra con el texto del CTA, centrado."""
    fondo = ColorClip(size=(ANCHO, ALTO), color=(0, 0, 0)).set_duration(duracion)
    texto = TextClip(
        TEXTO_CTA,
        fontsize=70,
        color="white",
        font="DejaVu-Sans-Bold",
        method="caption",
        size=(ANCHO - 160, None),
        align="center",
    ).set_duration(duracion).set_position("center")
    return CompositeVideoClip([fondo, texto])


# ---------------------------------------------------------------------------
# Generacion de titulo / hashtags
# ---------------------------------------------------------------------------

def generar_metadatos_con_gemini(frase: str):
    """Intenta usar Gemini. Si falla o no hay API key, usa el fallback local."""
    if not GEMINI_API_KEY:
        return generar_metadatos_fallback(frase)
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        modelo = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Genera un titulo corto y llamativo para un YouTube Short en español "
            f"basado en esta frase motivacional/reflexiva: \"{frase}\". "
            "Tambien genera 8 hashtags relevantes en español (sin espacios, con #). "
            "Responde en formato JSON con las claves 'titulo' y 'hashtags' (lista)."
        )
        respuesta = modelo.generate_content(prompt)
        texto = respuesta.text.strip()
        texto = re.sub(r"^```json|```$", "", texto, flags=re.MULTILINE).strip()
        datos = json.loads(texto)
        titulo = datos.get("titulo", "").strip()
        hashtags = datos.get("hashtags", [])
        if titulo and hashtags:
            return titulo, hashtags
        return generar_metadatos_fallback(frase)
    except Exception as e:
        print(f"[WARN] Fallback de metadatos (Gemini fallo): {e}")
        return generar_metadatos_fallback(frase)


def generar_metadatos_fallback(frase: str):
    """Fallback 100% local, sin IA, a costo cero."""
    titulo = frase.strip()
    if not titulo.endswith((".", "!", "?")):
        titulo += "."
    titulo = f"{titulo} #shorts"

    hashtags_base = [
        "#shorts", "#reflexion", "#motivacion", "#frases",
        "#eladooscurodelamente", "#psicologia", "#crecimientopersonal",
        "#verdades",
    ]
    random.shuffle(hashtags_base)
    return titulo, hashtags_base


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------

def obtener_credenciales_drive():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["DRIVE_REFRESH_TOKEN"],
        client_id=os.environ["DRIVE_CLIENT_ID"],
        client_secret=os.environ["DRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds


def obtener_credenciales_youtube():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds


def buscar_id_subcarpeta(drive_service, nombre: str, carpeta_padre_id: str):
    query = (
        f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{carpeta_padre_id}' in parents and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    archivos = resultado.get("files", [])
    if not archivos:
        raise RuntimeError(f"No se encontro la carpeta '{nombre}' dentro de la carpeta raiz.")
    return archivos[0]["id"]


def crear_subcarpeta_si_no_existe(drive_service, nombre: str, carpeta_padre_id: str):
    query = (
        f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{carpeta_padre_id}' in parents and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    archivos = resultado.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [carpeta_padre_id],
    }
    carpeta = drive_service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def obtener_imagen_mas_antigua(drive_service, carpeta_pendientes_id: str):
    query = (
        f"'{carpeta_pendientes_id}' in parents and trashed = false "
        "and (mimeType = 'image/jpeg' or mimeType = 'image/png')"
    )
    resultado = drive_service.files().list(
        q=query,
        fields="files(id, name, createdTime)",
        orderBy="createdTime",
    ).execute()
    archivos = resultado.get("files", [])
    if not archivos:
        return None
    return archivos[0]  # el mas antiguo primero por orderBy=createdTime


def descargar_archivo_drive(drive_service, file_id: str, ruta_destino: Path):
    request = drive_service.files().get_media(fileId=file_id)
    with open(ruta_destino, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        listo = False
        while not listo:
            _, listo = downloader.next_chunk()


def subir_archivo_drive(drive_service, ruta_local: Path, carpeta_id: str, nombre: str = None):
    metadata = {"name": nombre or ruta_local.name, "parents": [carpeta_id]}
    media = MediaFileUpload(str(ruta_local), resumable=True)
    archivo = drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
    return archivo["id"]


def mover_archivo_drive(drive_service, file_id: str, carpeta_origen_id: str, carpeta_destino_id: str):
    drive_service.files().update(
        fileId=file_id,
        addParents=carpeta_destino_id,
        removeParents=carpeta_origen_id,
        fields="id, parents",
    ).execute()


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def publicar_en_youtube(youtube_service, ruta_video: Path, titulo: str, hashtags: list, frase: str):
    descripcion = f"{frase}\n\n" + " ".join(hashtags)
    body = {
        "snippet": {
            "title": titulo[:100],
            "description": descripcion[:5000],
            "tags": [h.replace("#", "") for h in hashtags],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(ruta_video), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube_service.videos().insert(part="snippet,status", body=body, media_body=media)
    respuesta = None
    while respuesta is None:
        _, respuesta = request.next_chunk()
    return respuesta["id"]


# ---------------------------------------------------------------------------
# Mecanismo de catch-up: 10 publicaciones/dia repartidas en horario activo
# ---------------------------------------------------------------------------

HORARIOS_CHILE = [
    (0, 0),
    (2, 30),
    (5, 0),
    (7, 30),
    (10, 0),
    (12, 0),
    (14, 30),
    (17, 0),
    (19, 30),
    (22, 0),
]
OFFSET_CHILE = datetime.timedelta(hours=-4)
MAX_PUBLICACIONES_POR_CORRIDA = 3  # limite de seguridad por ejecucion del workflow


def calcular_publicaciones_pendientes(drive_service, carpeta_videos_gen_id: str) -> int:
    """
    Compara la hora actual (Chile) contra los 10 horarios fijos de
    HORARIOS_CHILE para saber cuantas publicaciones deberian haberse hecho
    hoy a esta hora, comparado con las que ya se publicaron (contando
    archivos en 'Videos Generados/YYYY-MM-DD/'). Si el workflow se salto
    ejecuciones, esto devuelve un numero > 1 para ponerse al dia.
    """
    ahora_utc = datetime.datetime.utcnow()
    ahora_chile = ahora_utc + OFFSET_CHILE
    hoy = ahora_chile.date().isoformat()

    objetivo_hasta_ahora = sum(
        1 for (h, m) in HORARIOS_CHILE
        if (ahora_chile.hour, ahora_chile.minute) >= (h, m)
    )

    query = (
        f"name = '{hoy}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{carpeta_videos_gen_id}' in parents and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    carpetas = resultado.get("files", [])
    ya_publicados = 0
    if carpetas:
        carpeta_hoy_id = carpetas[0]["id"]
        q2 = f"'{carpeta_hoy_id}' in parents and trashed = false and mimeType = 'video/mp4'"
        r2 = drive_service.files().list(q=q2, fields="files(id)").execute()
        ya_publicados = len(r2.get("files", []))

    pendientes = max(0, objetivo_hasta_ahora - ya_publicados)
    print(
        f"[catch-up] objetivo hasta ahora: {objetivo_hasta_ahora} | "
        f"ya publicados hoy: {ya_publicados} | pendientes: {pendientes}"
    )
    return min(pendientes, MAX_PUBLICACIONES_POR_CORRIDA)


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def procesar_un_short(drive_service, carpeta_pendientes_id, carpeta_publicadas_id,
                       carpeta_videos_gen_id) -> bool:
    """Procesa y publica un unico short. Devuelve False si no habia imagenes pendientes."""
    imagen_info = obtener_imagen_mas_antigua(drive_service, carpeta_pendientes_id)
    if imagen_info is None:
        print("No hay imagenes pendientes en Drive. Nada que hacer.")
        return False

    nombre_archivo = imagen_info["name"]
    file_id = imagen_info["id"]
    frase = frase_desde_nombre_archivo(nombre_archivo)
    print(f"Imagen seleccionada: {nombre_archivo}")
    print(f"Frase extraida: {frase}")

    ruta_imagen_local = TMP_DIR / nombre_archivo
    descargar_archivo_drive(drive_service, file_id, ruta_imagen_local)

    # --- Narracion principal ---
    ruta_tts_crudo = TMP_DIR / "narracion_cruda.mp3"
    asyncio.run(generar_audio_tts(frase, ruta_tts_crudo))
    ruta_narracion_final = TMP_DIR / "narracion_final.mp3"
    agregar_silencios(ruta_tts_crudo, ruta_narracion_final, SILENCIO_INICIO, SILENCIO_FIN)

    audio_principal = AudioFileClip(str(ruta_narracion_final))
    duracion_principal = audio_principal.duration

    clip_imagen = crear_clip_imagen_fija(ruta_imagen_local, duracion_principal)
    clip_imagen = clip_imagen.set_audio(audio_principal)

    # --- CTA final ---
    ruta_tts_cta = TMP_DIR / "cta_crudo.mp3"
    asyncio.run(generar_audio_tts(TEXTO_CTA, ruta_tts_cta))
    audio_cta = AudioFileClip(str(ruta_tts_cta))
    clip_cta = crear_clip_cta(audio_cta.duration + 0.5)
    clip_cta = clip_cta.set_audio(audio_cta.set_start(0.2))

    # --- Video final ---
    video_final = concatenate_videoclips([clip_imagen, clip_cta], method="compose")
    ruta_video_final = TMP_DIR / f"short_final_{file_id}.mp4"
    video_final.write_videofile(
        str(ruta_video_final), fps=30, codec="libx264", audio_codec="aac", logger=None
    )

    # --- Metadatos ---
    titulo, hashtags = generar_metadatos_con_gemini(frase)
    print(f"Titulo generado: {titulo}")
    print(f"Hashtags: {hashtags}")

    # --- Publicar en YouTube ---
    creds_youtube = obtener_credenciales_youtube()
    youtube_service = build("youtube", "v3", credentials=creds_youtube)
    video_id = publicar_en_youtube(youtube_service, ruta_video_final, titulo, hashtags, frase)
    print(f"Publicado en YouTube: https://youtube.com/shorts/{video_id}")

    # --- Guardar copia en Drive ---
    hoy = datetime.date.today().isoformat()
    carpeta_fecha_id = crear_subcarpeta_si_no_existe(drive_service, hoy, carpeta_videos_gen_id)
    nombre_video_drive = f"{Path(nombre_archivo).stem}.mp4"
    subir_archivo_drive(drive_service, ruta_video_final, carpeta_fecha_id, nombre_video_drive)
    print(f"Copia guardada en Drive: Videos Generados/{hoy}/{nombre_video_drive}")

    # --- Mover imagen origen a Publicadas ---
    mover_archivo_drive(drive_service, file_id, carpeta_pendientes_id, carpeta_publicadas_id)
    print("Imagen origen movida a Publicadas/")
    return True


def main():
    print("== El Lado Oscuro: generador de shorts ==")

    creds_drive = obtener_credenciales_drive()
    drive_service = build("drive", "v3", credentials=creds_drive)

    carpeta_pendientes_id = buscar_id_subcarpeta(drive_service, CARPETA_PENDIENTES, DRIVE_FOLDER_ID_RAIZ)
    carpeta_publicadas_id = crear_subcarpeta_si_no_existe(drive_service, CARPETA_PUBLICADAS, DRIVE_FOLDER_ID_RAIZ)
    carpeta_videos_gen_id = crear_subcarpeta_si_no_existe(drive_service, CARPETA_VIDEOS_GENERADOS, DRIVE_FOLDER_ID_RAIZ)

    pendientes = calcular_publicaciones_pendientes(drive_service, carpeta_videos_gen_id)
    if pendientes == 0:
        print("Nada pendiente por ahora segun el horario objetivo. Fin.")
        return

    publicados_en_esta_corrida = 0
    for _ in range(pendientes):
        hubo_imagen = procesar_un_short(
            drive_service, carpeta_pendientes_id, carpeta_publicadas_id, carpeta_videos_gen_id
        )
        if not hubo_imagen:
            break
        publicados_en_esta_corrida += 1

    print(f"== Proceso completado. Publicados en esta corrida: {publicados_en_esta_corrida} ==")


if __name__ == "__main__":
    main()

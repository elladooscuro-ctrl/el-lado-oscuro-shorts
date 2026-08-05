"""
El Lado Oscuro - Generador automatico de YouTube Shorts
=========================================================
Flujo (material desde Drive: carpeta VIDEO + carpeta TEXTO):
1. Elegir la frase con el numero mas pequeno disponible en la carpeta
   "TEXTO" de Drive (archivo Word o Google Doc), leer el TEXTO QUE
   CONTIENE (no el nombre del archivo), y moverla a "TEXTO/Usadas"
   para que no se vuelva a usar
2. Elegir un video de fondo de la carpeta "VIDEO" de Drive: uno por
   cada corrida del dia (0 al 9). Si Jose agrega mas videos para el
   mismo numero de corrida usando sufijos decimales (0, 0.1, 0.0.1,
   0.0.0.1, ...), se elige uno al azar entre esas variantes.
3. Generar narracion con edge-tts (voz es-MX-JorgeNeural), sin silencio
   antes de la frase (arranca a hablar de inmediato) y con 0.3s de
   silencio despues de la frase
4. Armar el video final (video de fondo + audio) con moviepy: el video
   se ajusta (loop o recorte) para durar exactamente lo mismo que el
   audio
5. Generar titulo y hashtags (Gemini, con fallback local sin IA)
6. Subir el video a YouTube (publico)
7. Guardar copia en Drive: "Videos Generados/YYYY-MM-DD/"

Lecciones aplicadas del proyecto anterior (reels-automatizados):
- moviepy fijado en 1.0.3 (versiones nuevas rompen la API usada aqui)
- Pillow: shim de Image.ANTIALIAS para compatibilidad con moviepy 1.0.3
- OAuth de Drive (no service account: sin cuota de almacenamiento propio)
- Disparo externo via cron-job.org + workflow_dispatch (ver .github/workflows)
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
import docx

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
    VideoFileClip,
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
SILENCIO_INICIO = 0  # segundos (sin silencio antes de hablar)
SILENCIO_FIN = 0.3  # segundos

CARPETA_VIDEOS_GENERADOS = "Videos Generados"

# Carpetas del material en Drive (cuenta El Lado Oscuro).
# VIDEO: videos de fondo numerados 0, 1, 2 ... 9 (y variantes con sufijo
# decimal, ej. "0.1", "0.0.1", para agregar mas videos sin renumerar todo).
VIDEO_FOLDER_ID = "1qzvm3II0gUFdW10jUK6_SSrOXX-yXufX"

# TEXTO: frases guardadas como archivos Word (.docx) o Google Doc, nombradas
# con un numero (1, 2, 3...). Siempre se usa el archivo con el numero mas
# chico que quede en la carpeta (no al azar), y despues de leer el TEXTO
# QUE CONTIENE se mueve a la subcarpeta "Usadas" para no repetirlo.
TEXTO_FOLDER_ID = "1TePqdnW0F6MXS5mnflNdBD-okds3SMsR"
NOMBRE_CARPETA_USADAS = "Usadas"

DRIVE_FOLDER_ID_RAIZ = os.environ.get("DRIVE_FOLDER_ID_RAIZ")  # carpeta raiz del proyecto en Drive
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ANCHO, ALTO = 1080, 1920  # formato vertical short

TMP_DIR = Path(tempfile.mkdtemp(prefix="lado_oscuro_"))

FRASE_RESPALDO = "Vuelve a esta parte de ti que sabe la verdad, aunque el resto del dia la ignore."

# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

async def generar_audio_tts(texto: str, ruta_salida: Path, voz: str = VOZ):
    comunicador = edge_tts.Communicate(texto, voz)
    await comunicador.save(str(ruta_salida))


def agregar_silencios(ruta_audio_in: Path, ruta_audio_out: Path,
                       silencio_inicio: float, silencio_fin: float):
    """Usa moviepy para anteponer/agregar silencio a un audio."""
    clip = AudioFileClip(str(ruta_audio_in))
    partes = [clip]
    if silencio_inicio > 0:
        silencio_i_path = TMP_DIR / "sil_i.mp3"
        _generar_silencio_mp3(silencio_i_path, silencio_inicio)
        partes.insert(0, AudioFileClip(str(silencio_i_path)))
    if silencio_fin > 0:
        silencio_f_path = TMP_DIR / "sil_f.mp3"
        _generar_silencio_mp3(silencio_f_path, silencio_fin)
        partes.append(AudioFileClip(str(silencio_f_path)))

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


def crear_clip_video_fondo(ruta_video: Path, duracion: float) -> VideoFileClip:
    """Prepara el video de fondo: sin audio, cubre el formato vertical, y
    se ajusta a la duracion exacta del audio (se repite en loop si es mas
    corto, se recorta si es mas largo)."""
    clip = VideoFileClip(str(ruta_video)).without_audio()
    clip = clip.resize(height=ALTO)
    if clip.w < ANCHO:
        clip = clip.resize(width=ANCHO)
    clip = clip.crop(
        x_center=clip.w / 2, y_center=clip.h / 2, width=ANCHO, height=ALTO
    )
    if clip.duration < duracion:
        n_loops = int(duracion // clip.duration) + 1
        clip = concatenate_videoclips([clip] * n_loops)
    clip = clip.subclip(0, duracion)
    return clip

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
        modelo = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "Genera un titulo corto y llamativo (maximo 6 palabras) para un "
            "YouTube Short en español, basado en esta frase motivacional/"
            f"reflexiva: \"{frase}\". "
            "Tambien genera 8 hashtags relevantes en español (sin espacios, con #). "
            "Responde en formato JSON con las claves 'titulo' y 'hashtags' (lista)."
        )
        respuesta = modelo.generate_content(prompt)
        texto = respuesta.text.strip()
        texto = re.sub(r"^```json|```$", "", texto, flags=re.MULTILINE).strip()
        datos = json.loads(texto)
        titulo = datos.get("titulo", "").strip()
        hashtags = datos.get("hashtags", [])
        if titulo and len(titulo.split()) > 6:
            titulo = " ".join(titulo.split()[:6])
        if titulo and hashtags:
            return titulo, hashtags
        return generar_metadatos_fallback(frase)
    except Exception as e:
        print(f"[WARN] Fallback de metadatos (Gemini fallo): {e}")
        return generar_metadatos_fallback(frase)


def generar_metadatos_fallback(frase: str):
    """Fallback 100% local, sin IA, a costo cero."""
    palabras = frase.strip().split()
    titulo = " ".join(palabras[:6])
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
# Seleccion de FRASE desde la carpeta TEXTO (numero mas pequeno primero)
# ---------------------------------------------------------------------------

def _numero_inicial(nombre_archivo: str):
    """Extrae el numero entero al inicio del nombre de archivo (sin
    extension), ej. '7.docx' -> 7, '12' -> 12. Devuelve None si el nombre
    no empieza con un numero."""
    base = re.sub(r"\.[A-Za-z0-9]+$", "", nombre_archivo)
    m = re.match(r"^(\d+)", base.strip())
    return int(m.group(1)) if m else None


def obtener_o_crear_carpeta_usadas(drive_service, carpeta_padre_id: str):
    return crear_subcarpeta_si_no_existe(drive_service, NOMBRE_CARPETA_USADAS, carpeta_padre_id)


def leer_texto_de_archivo(drive_service, archivo: dict) -> str:
    """Devuelve el TEXTO QUE CONTIENE el archivo (no el nombre): Google Doc
    exportado a texto plano, o .docx leido con python-docx."""
    mime = archivo["mimeType"]
    if mime == "application/vnd.google-apps.document":
        data = drive_service.files().export(fileId=archivo["id"], mimeType="text/plain").execute()
        texto = data.decode("utf-8") if isinstance(data, bytes) else data
    else:
        ruta_local = TMP_DIR / f"texto_{archivo['id']}.docx"
        descargar_archivo_drive(drive_service, archivo["id"], ruta_local)
        documento = docx.Document(str(ruta_local))
        texto = "\n".join(p.text for p in documento.paragraphs)
    return texto.strip()


def obtener_frase_y_marcarla_usada(drive_service):
    """Busca en la carpeta TEXTO el archivo (Word o Google Doc) cuyo
    nombre empiece con el numero mas chico, lee el TEXTO QUE CONTIENE, lo
    mueve a la subcarpeta 'Usadas' (para no repetirlo nunca) y devuelve la
    frase. Si la carpeta esta vacia, devuelve None."""
    query = f"'{TEXTO_FOLDER_ID}' in parents and trashed = false"
    resultado = drive_service.files().list(
        q=query, fields="files(id, name, mimeType)", pageSize=1000
    ).execute()
    archivos = resultado.get("files", [])

    candidatos = [(a, _numero_inicial(a["name"])) for a in archivos]
    candidatos = [(a, n) for a, n in candidatos if n is not None]
    if not candidatos:
        print("[WARN] La carpeta TEXTO no tiene archivos numerados disponibles.")
        return None

    archivo_elegido, _ = min(candidatos, key=lambda par: par[1])
    print(f"Frase elegida de TEXTO: '{archivo_elegido['name']}' (numero mas chico disponible)")

    frase = leer_texto_de_archivo(drive_service, archivo_elegido)

    carpeta_usadas_id = obtener_o_crear_carpeta_usadas(drive_service, TEXTO_FOLDER_ID)
    mover_archivo_drive(drive_service, archivo_elegido["id"], TEXTO_FOLDER_ID, carpeta_usadas_id)
    print(f"'{archivo_elegido['name']}' movido a TEXTO/{NOMBRE_CARPETA_USADAS}")

    return frase

# ---------------------------------------------------------------------------
# Seleccion de VIDEO desde la carpeta VIDEO (indice de corrida 0-9)
# ---------------------------------------------------------------------------

def _numero_base_video(nombre_archivo: str):
    """'7.mp4' -> 7 | '0.1.mp4' -> 0 | '0.0.1' -> 0 | 'clip.mp4' -> None."""
    base = re.sub(r"\.[A-Za-z0-9]+$", "", nombre_archivo)
    primer_tramo = base.split(".")[0].strip()
    return int(primer_tramo) if primer_tramo.isdigit() else None


def elegir_y_descargar_video(drive_service, indice: int) -> Path:
    """Elige un video de la carpeta VIDEO cuyo numero base coincida con
    `indice` (0-9). Si existen variantes decimales del mismo numero
    (0, 0.1, 0.0.1...) elige una al azar entre ellas, para poder agregar
    mas videos sin romper la numeracion."""
    query = f"'{VIDEO_FOLDER_ID}' in parents and trashed = false and mimeType contains 'video/'"
    resultado = drive_service.files().list(
        q=query, fields="files(id, name)", pageSize=1000
    ).execute()
    videos = resultado.get("files", [])
    if not videos:
        raise RuntimeError("La carpeta VIDEO de Drive esta vacia.")

    candidatos = [v for v in videos if _numero_base_video(v["name"]) == indice]
    if not candidatos:
        print(f"[WARN] No hay video para el numero {indice}, se elige uno al azar de toda la carpeta.")
        candidatos = videos

    elegido = random.choice(candidatos)
    print(f"Video de fondo elegido: {elegido['name']} (indice {indice})")

    ruta_local = TMP_DIR / elegido["name"]
    descargar_archivo_drive(drive_service, elegido["id"], ruta_local)
    return ruta_local

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

PUBLICACIONES_POR_DIA = 10
HORA_INICIO_DIA = 0  # 08:00
HORA_FIN_DIA = 24  # 23:00
MAX_PUBLICACIONES_POR_CORRIDA = 3  # limite de seguridad por ejecucion del workflow


def calcular_publicaciones_pendientes(drive_service, carpeta_videos_gen_id: str):
    """
    Calcula cuantos videos deberian haberse publicado hoy a esta hora,
    comparado con los que ya se publicaron (contando archivos en
    'Videos Generados/YYYY-MM-DD/'). Devuelve (pendientes, ya_publicados):
    ya_publicados tambien se usa para saber que indice de video (0-9)
    corresponde a cada corrida del dia.
    """
    ahora = datetime.datetime.now()
    hoy = ahora.date().isoformat()

    minutos_totales = (HORA_FIN_DIA - HORA_INICIO_DIA) * 60
    minutos_transcurridos = max(
        0, (ahora.hour - HORA_INICIO_DIA) * 60 + ahora.minute
    )
    minutos_transcurridos = min(minutos_transcurridos, minutos_totales)

    if ahora.hour < HORA_INICIO_DIA:
        objetivo_hasta_ahora = 0
    else:
        objetivo_hasta_ahora = round(
            (minutos_transcurridos / minutos_totales) * PUBLICACIONES_POR_DIA
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
    return min(pendientes, MAX_PUBLICACIONES_POR_CORRIDA), ya_publicados

# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def procesar_un_short(drive_service, carpeta_videos_gen_id: str, indice: int) -> bool:
    """Procesa y publica un unico short usando el video `indice` (0-9).
    Devuelve False si no habia frase disponible en TEXTO."""
    frase = obtener_frase_y_marcarla_usada(drive_service)
    if frase is None:
        frase = FRASE_RESPALDO

    print(f"Frase: {frase}")

    ruta_video_local = elegir_y_descargar_video(drive_service, indice)

    # --- Narracion principal ---
    ruta_tts_crudo = TMP_DIR / "narracion_cruda.mp3"
    asyncio.run(generar_audio_tts(frase, ruta_tts_crudo))
    ruta_narracion_final = TMP_DIR / "narracion_final.mp3"
    agregar_silencios(ruta_tts_crudo, ruta_narracion_final, SILENCIO_INICIO, SILENCIO_FIN)

    audio_principal = AudioFileClip(str(ruta_narracion_final))
    duracion_principal = audio_principal.duration

    clip_video = crear_clip_video_fondo(ruta_video_local, duracion_principal)
    clip_video = clip_video.set_audio(audio_principal)

    # --- Video final ---
    video_final = clip_video
    ruta_video_final = TMP_DIR / f"short_final_{indice}_{random.randint(1000, 9999)}.mp4"
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
    nombre_video_drive = f"short_{hoy}_{indice}.mp4"
    subir_archivo_drive(drive_service, ruta_video_final, carpeta_fecha_id, nombre_video_drive)
    print(f"Copia guardada en Drive: Videos Generados/{hoy}/{nombre_video_drive}")

    return True


def main():
    print("== El Lado Oscuro: generador de shorts ==")

    creds_drive = obtener_credenciales_drive()
    drive_service = build("drive", "v3", credentials=creds_drive)

    carpeta_videos_gen_id = crear_subcarpeta_si_no_existe(
        drive_service, CARPETA_VIDEOS_GENERADOS, DRIVE_FOLDER_ID_RAIZ
    )

    pendientes, ya_publicados_hoy = calcular_publicaciones_pendientes(drive_service, carpeta_videos_gen_id)
    if os.environ.get("FORZAR") == "true" and pendientes == 0:
        print("[FORZAR] Se fuerza 1 publicacion aunque no toque por horario.")
        pendientes = 1
    if pendientes == 0:
        print("Nada pendiente por ahora segun el horario objetivo. Fin.")
        return

    publicados_en_esta_corrida = 0
    for i in range(pendientes):
        # Indice de video 0-9: la publicacion N del dia usa el video N-1
        # (0-indexado). Al llegar a la publicacion 11 (dia siguiente),
        # vuelve a 0: es un ciclo por dia, no acumulado entre dias.
        indice = (ya_publicados_hoy + i) % 10
        hubo_frase = procesar_un_short(drive_service, carpeta_videos_gen_id, indice)
        if not hubo_frase:
            break
        publicados_en_esta_corrida += 1

    print(f"== Proceso completado. Publicados en esta corrida: {publicados_en_esta_corrida} ==")


if __name__ == "__main__":
    main()

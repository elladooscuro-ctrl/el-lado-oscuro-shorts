# El Lado Oscuro de la Mente - Shorts automaticos

Genera y publica automaticamente Shorts para el canal de YouTube
**"El Lado Oscuro de la Mente"**. Cada corrida arma un video (video de
fondo + narracion en voz + titulo/hashtags) y lo publica solo, sin
intervencion manual.

## Como funciona cada corrida

1. Se elige la **frase** con el numero mas chico disponible en la
   carpeta `TEXTO` de Drive, se lee el texto que contiene el archivo
   (no el nombre) y se mueve a `TEXTO/Usadas` para que no se repita.
2. Se elige un **video de fondo** de la carpeta `VIDEO` de Drive segun
   el numero de corrida del dia (0 al 9, ciclico).
3. Se genera la narracion con voz `es-MX-JorgeNeural` (sin silencio al
   inicio, 0.3s de silencio al final).
4. Se arma el video final (el video de fondo se ajusta con loop o
   recorte para durar exactamente lo mismo que el audio).
5. Se genera un titulo corto (maximo 6 palabras) y 8 hashtags con
   Gemini; si Gemini falla, hay un respaldo local sin IA.
6. Se publica en YouTube (publico) y se guarda una copia en Drive en
   `Videos Generados/AAAA-MM-DD/`.

Se publican hasta 10 videos por dia, repartidos automaticamente segun
la hora (mecanismo de "catch-up": si el workflow se salta una
ejecucion, se pone al dia solo, sin duplicar publicaciones).

## Como subir material nuevo (para Jose)

### Agregar videos de fondo

Carpeta de Drive **VIDEO**:
https://drive.google.com/drive/u/2/folders/1qzvm3II0gUFdW10jUK6_SSrOXX-yXufX

- Los videos deben nombrarse con un numero del **0 al 9** (ej. `0.mp4`,
  `1.mp4`... `9.mp4`). Cada numero corresponde a una corrida del dia.
- Si quieres agregar mas de un video para el mismo numero de corrida,
  usa sufijos decimales: `0.1.mp4`, `0.2.mp4`, `0.0.1.mp4`, etc. El
  sistema elige al azar entre todas las variantes que compartan el
  mismo numero inicial. Esto nunca genera errores ni hace que se
  repita un video de forma incorrecta.
- Los videos no se borran ni se mueven despues de usarse: se pueden
  volver a usar en corridas futuras.

### Agregar frases

Carpeta de Drive **TEXTO**:
https://drive.google.com/drive/u/2/folders/1TePqdnW0F6MXS5mnflNdBD-okds3SMsR

- Cada frase va en un archivo Word (`.docx`) o Google Doc, nombrado
  con un numero (`1`, `2`, `3`...). El texto que se usa es el
  **contenido del archivo**, no el nombre.
- Siempre se usa el archivo con el numero mas chico disponible en la
  carpeta (nunca al azar).
- Una vez usado, el archivo se mueve automaticamente a la subcarpeta
  `TEXTO/Usadas` (una sola carpeta, sin subcarpetas por fecha) para
  que no se repita.
- Cuando se acaban las frases numeradas, se usa una frase de respaldo
  fija hasta que se agreguen mas.

### Donde queda el video final

Cada video publicado se guarda tambien en Drive, en:
`Videos Generados/AAAA-MM-DD/short_AAAA-MM-DD_<indice>.mp4`

## Ejecucion manual / prueba

En GitHub: pestaña **Actions** → workflow **"Publicar Shorts - El Lado
Oscuro"** → botón **Run workflow**. Ahí hay un campo opcional
**"forzar"**: si se pone en `true`, se genera y publica un video de
inmediato aunque no toque todavia por horario (util para pruebas).

## Archivos principales del repo

- `generar_short.py`: script principal (todo el pipeline).
- `requirements.txt`: dependencias de Python (incluye `python-docx`
  para leer los archivos `.docx` de la carpeta TEXTO).
- `.github/workflows/publicar_shorts.yml`: workflow de GitHub Actions
  que corre el script cada 15 minutos (cron) y tambien se puede
  disparar manualmente.

## Notas tecnicas

- El modelo de Gemini usado para titulo/hashtags es
  `gemini-flash-latest` (alias que siempre apunta al modelo Flash
  vigente, para no tener que actualizar el nombre cuando Google
  descontinua versiones).
- moviepy esta fijado en la version 1.0.3 porque versiones mas nuevas
  rompen la API que usa este script.
- Las credenciales de Google Drive y YouTube usan OAuth (no service
  account), guardadas como secrets del repo.

# 🔥 Stocks on fire

Herramienta gratuita y de código abierto para **investigar acciones de EE. UU. con los datos públicos de la SEC**, en español:

- **Cuántas gestoras institucionales tienen cada acción**, trimestre a trimestre desde 2013 (formulario 13F), quién la tiene y quién entra o sale.
- **Compras y ventas de directivos** (formulario 4) desde 2006: quién, cuánto y cuánto cambia su participación.
- **Ranking** de las acciones donde más gestoras están entrando, con detección automática de splits.
- **Pestaña «Stocks on fire»**: una puntuación que combina la entrada de gestoras (y su aceleración) con el momento técnico del precio.
- **Valoración**: PER forward, PER actual, beneficio por acción esperado y PEG, con filtros (solo informativa, no puntúa).
- **Fundamentales de la SEC** (estados financieros, últimos 12 meses): ventas, beneficio, márgenes, flujo de caja libre, PER y PEG histórico, y su crecimiento. Datos públicos y con fecha de publicación, así que sí se han podido validar.
- Ficha completa de cada acción con gráficos, favoritos, exportación a CSV, modo oscuro y actualización automática.

Funciona **en tu ordenador**: no hay servidores, cuentas ni suscripciones. Tú descargas los datos directamente de la SEC.

> ⚠️ **No es una recomendación de inversión.** Es una herramienta de consulta. Los backtests incluidos muestran que estos datos, por sí solos, **no dan una ventaja operable** (ver [«¿Funciona?»](#funciona)). Úsala para investigar, no para comprar a ciegas.

---

## Requisitos

- **Python 3.11 o superior** (probado con Python 3.14 y pandas 3.0) — descárgalo de [python.org](https://www.python.org/downloads/).
- Unos **7 GB de espacio libre** y conexión a internet.
- Un navegador moderno (Chrome, Edge, Firefox, Safari).

## Instalación

1. Descarga el proyecto: botón verde **Code → Download ZIP** en GitHub (y descomprímelo), o con git:

   ```bash
   git clone https://github.com/carcaiso-max/stocks-on-fire.git
   ```

2. Instala las dos librerías que necesita, desde una consola en la carpeta del proyecto:

   ```bash
   pip install -r requirements.txt
   ```

## Arrancar la app

- **Windows:** doble clic en `iniciar.bat`.
- **Mac / Linux:** `sh iniciar.sh` (o `python3 app_13f.py`).

Se abrirá el navegador en `http://127.0.0.1:8613`. La ventana de la consola es el servidor: **déjala abierta mientras uses la app**. Si la cierras, la app deja de responder.

### La primera vez

La app te mostrará una pantalla de bienvenida para **descargar los datos** (unos 5,5 GB; tarda alrededor de una hora). Antes te pedirá tu **nombre y email**:

> La SEC exige que quien descarga sus datos de forma automática se identifique ([normas de acceso de la SEC](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)). No es un registro ni una cuenta: tu nombre y email van dentro de cada petición a sec.gov para que puedan contactar si un programa da problemas. **Se guardan solo en tu ordenador y únicamente se envían a sec.gov.** Puedes cambiarlos en cualquier momento desde el pie de la app.

También puedes descargar los datos desde la consola: `python build_13f.py` (te pedirá la identificación si falta).

## Mantenerla al día

- **Botón «Buscar actualizaciones»**: comprueba si la SEC ha publicado ficheros nuevos y los descarga.
- **Interruptor «Auto»**: comprueba la SEC una vez al día y actualiza sola mientras el servidor esté encendido.

La SEC publica los datos cada trimestre, así que la mayoría de los días no habrá nada nuevo.

## Dónde se guardan los datos

En la carpeta `fondos13f` dentro de tu carpeta de usuario (por ejemplo `C:\Users\tu_usuario\fondos13f`). Para usar otra carpeta, define la variable de entorno `FONDOS13F_DIR`. Para usar otro puerto, `FONDOS13F_PORT`.

---

## Qué muestra y cómo leerlo

- **Gestoras, no fondos.** El 13F lo presentan las gestoras con más de 100 M$ en acciones de EE. UU. (BlackRock cuenta como una aunque tenga cientos de fondos). Solo posiciones largas; se excluyen opciones.
- **Retraso.** El 13F se presenta hasta 45 días después de cada trimestre: los datos llegan con 1,5 a 4,5 meses de retraso. La columna **«a +45 d»** cuenta solo lo que se sabía a tiempo; úsala si haces backtests, para no mirar al futuro.
- **Splits.** Las acciones declaradas no vienen ajustadas por splits. El ranking los detecta solo (si muchas gestoras que no han operado tienen exactamente ×10 acciones, es un split 10×1).
- **Directivos.** Solo compras y ventas en mercado abierto. Las ventas «plan» (regla 10b5-1) están programadas con antelación y dicen poco.
- **Precios** de Yahoo Finance, solo para el análisis técnico (máximos de 52 semanas, medias, fuerza relativa).
- **Valoración** (PER forward, PEG) de Yahoo Finance, con estimaciones de analistas: suelen ser optimistas, y si se esperan pérdidas no hay PER forward ni PEG con sentido. Compárala dentro del mismo sector. No hay histórico gratuito de estimaciones, así que no está validada con backtest.

## ¿Funciona?

Incluyo los backtests completos para que cada uno saque sus conclusiones (en la carpeta [`resultados/`](resultados/)). Resumen, siempre sin mirar al futuro:

| Qué se probó | Periodo | Resultado |
|---|---|---|
| Top 20 por entrada de gestoras (13F), a 6 meses | 2014–2026 | +4,4 puntos sobre la media (t = 2,2), pero casi todo en 2022–2026; débil antes |
| Solo **aceleración** de gestoras, top 20, como cartera con costes | 2014–2026 | 14,9 % anual vs 12,0 % de SPY, pero con más riesgo: **Sharpe 0,63 vs 0,74 de SPY**, caída máxima −48 % |
| Parte técnica (cerca de máximos, medias, fuerza relativa) | 2014–2026 | No añade ventaja |
| **Aceleración + ventas que crecen más del 10 %** (estados financieros SEC), top 20, cartera con costes | 2014–2026 | **19,7 % anual vs 12,0 % de SPY, Sharpe 0,78 vs 0,74**; gana a SPY en los tres tramos y en 8 de 12 años (sin 2020: 16,5 % vs 11,3 %). Caída máxima −46 % vs −34 %. Es la única combinación que mejora al índice ajustando por riesgo, pero se probaron varias antes de dar con ella |
| Valoración barata (PER bajo, flujo de caja, valor contable) | 2014–2026 | **Empeora**: las acciones más baratas rindieron menos (las 20 más baratas por PER: 3 % anual) |
| **Compras de directivos** (5 variantes: CEO/CFO, grupos de directivos, aumentos de participación…) | 2006–2026 | Sin ventaja: ~0 frente al Russell 2000; la ventaja que hubo antes de 2013 ha desaparecido |

**Conclusión honesta:** los datos públicos de la SEC son información excelente para entender una acción, pero el mercado los incorpora rápido y, por separado, no baten al índice ajustando por riesgo. La única combinación que lo consigue en el backtest (entrada acelerada de gestoras + ventas creciendo) es la que usa por defecto la pestaña «Stocks on fire»; trátala como hipótesis prometedora, no como garantía. Los backtests tienen además sesgo de supervivencia (faltan precios de empresas desaparecidas), lo que los hace, si acaso, optimistas.

Para reproducirlos (requieren los datos ya descargados):

```bash
python backtest_13f.py              # puntuación 13F por quintiles y top 20
python estrategia_aceleracion.py    # cartera de aceleración con costes
python historial_flujos.py          # histórico de convicción y compradores/vendedores (para backtest_13f)
python backtest_insiders.py         # compras de directivos
python fundamentales.py             # estados financieros de la SEC (1,4 GB)
python backtest_fundamentales.py    # valoración, crecimiento y filtros sobre la aceleración
```

## Estructura del proyecto

| Fichero | Para qué sirve |
|---|---|
| `app_13f.py` | Servidor local de la app (solo biblioteca estándar + pandas) |
| `index.html` | La interfaz |
| `build_13f.py` | Descarga los datos 13F de la SEC y construye la base de datos |
| `insiders.py` | Descarga las compras y ventas de directivos (formulario 4) |
| `precios.py` | Precios diarios e indicadores técnicos |
| `valoracion.py` | PER forward, PER actual, PEG y sector (Yahoo) |
| `fundamentales.py` | Estados financieros de la SEC (XBRL) en 12 meses móviles, con fecha de publicación |
| `backtest_*.py`, `estrategia_aceleracion.py`, `historial_flujos.py` | Backtests |
| `iniciar.bat` / `iniciar.sh` | Arranque con doble clic |
| `actualizar_datos.bat` | Actualizar desde la consola (Windows) |

## Fuentes de datos y licencias

- **SEC** (13F, formulario 4, estados financieros XBRL, lista de tickers): datos públicos del Gobierno de EE. UU.
- **Yahoo Finance** (precios y valoración): API no oficial, **solo para uso personal**. Este proyecto no está afiliado a Yahoo; cada usuario descarga los precios para su propio uso. No redistribuyas esos datos.
- **OpenFIGI** (equivalencia entre CUSIP y ticker): identificadores abiertos de Bloomberg.
- **Chart.js** (gráficos), licencia MIT.

Este repositorio **no contiene datos**: solo el código para descargarlos.

## Limitaciones conocidas

- Si una empresa cambia de CUSIP (fusiones, reorganizaciones), su historial se corta. Ejemplo: Google pasó a Alphabet en 2015.
- Los ficheros de la SEC tienen algún trimestre incompleto; afecta por igual a todas las acciones de ese trimestre.
- Las acciones de tickers poco habituales (clases B, extranjeras) a veces no se encuentran por ticker; prueba por nombre o CUSIP.

## Del mismo autor

**BrokkoPay**, para seguir el patrimonio de todos tus brókeres y ver el «sueldo» mensual que generan tus inversiones. [Google Play](https://play.google.com/store/apps/details?id=com.brokkopay.app) · *Promoción del autor*

## Licencia

[MIT](LICENSE): puedes usarla, modificarla y compartirla libremente. Se distribuye sin garantía de ningún tipo.

**Aviso:** esta herramienta es solo informativa y educativa. No constituye asesoramiento ni recomendación de inversión. Invertir conlleva riesgo de pérdida.

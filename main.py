import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from groq import Groq
import json
import time
import random
from datetime import datetime, timezone
import urllib.parse
import re
import os

from config import *
from redes_sociales import publicar_en_square, enviar_telegram, enviar_foto_telegram

# Validación básica de seguridad
if not GROQ_API_KEY:
    raise ValueError("❌ Error: La variable GROQ_API_KEY no está configurada.")
if not MODO_PRUEBA and not SQUARE_API_KEY:
    raise ValueError("❌ Error: SQUARE_API_KEY es necesaria para publicar en Binance (MODO_PRUEBA=False). Revisa tus Secretos en GitHub.")
client = Groq(api_key=GROQ_API_KEY)

# --- CONFIGURACIÓN DE RED AVANZADA ---
# Reutiliza conexiones TCP (más rápido) y añade reintentos automáticos si hay micro-cortes.
sesion_http = requests.Session()
reintentos = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
sesion_http.mount('https://', HTTPAdapter(max_retries=reintentos))
sesion_http.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

def generar_texto_ia(prompt, temperatura=0.7):
    """Función centralizada para interactuar con el modelo de IA de Groq."""
    try:
        print(f"🤖 Conectando con Groq (Modelo: {GROQ_MODEL_NAME})...")
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL_NAME,
            temperature=temperatura
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        print(f"⚠️ Error generando texto con IA: {e}")
        return None

def cargar_historial():
    if os.path.exists(ARCHIVO_HISTORIAL):
        try:
            with open(ARCHIVO_HISTORIAL, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ Advertencia: El archivo {ARCHIVO_HISTORIAL} está corrupto o vacío. Se creará uno nuevo.")
            return {}
    return {}

def guardar_historial(symbol):
    historial = cargar_historial()
    historial[symbol] = time.time()
    # Limpieza: Eliminar entradas de más de 24 horas (86400 segundos)
    limite = time.time() - 86400
    historial = {k: v for k, v in historial.items() if v > limite}
    
    with open(ARCHIVO_HISTORIAL, "w") as f:
        json.dump(historial, f, indent=4)

def obtener_datos_coingecko(symbol):
    """Backup: Obtiene precio y cambio 24h desde CoinGecko."""
    try:
        cg_id = COINGECKO_IDS.get(symbol)
        if not cg_id: return None
        
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {'ids': cg_id, 'vs_currencies': 'usd', 'include_24hr_change': 'true'}
        resp = sesion_http.get(url, params=params, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            if cg_id in data:
                # Adaptamos formato para que sea idéntico al de Binance
                return {
                    'lastPrice': data[cg_id]['usd'],
                    'priceChangePercent': data[cg_id]['usd_24h_change']
                }
    except Exception as e:
        print(f"⚠️ Error CoinGecko ({symbol}): {e}")
    return None

def obtener_fomo_coingecko(symbol):
    """Obtiene el porcentaje de sentimiento alcista (FOMO) de la comunidad en CoinGecko."""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id: return None
    
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}"
        # Pedimos solo lo básico para no saturar la API
        params = {'localization': 'false', 'tickers': 'false', 'market_data': 'false', 'community_data': 'false', 'developer_data': 'false', 'sparkline': 'false'}
        resp = sesion_http.get(url, params=params, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            fomo = data.get('sentiment_votes_up_percentage')
            return float(fomo) if fomo is not None else None
    except Exception as e:
        print(f"⚠️ Error obteniendo FOMO social para {symbol}: {e}")
    return None

def analizar_oportunidades():
    """
    Analiza la lista MONEDAS_ANALISIS y selecciona la MEJOR oportunidad basada en:
    1. RSI Extremo (Prioridad): < 30 (Sobreventa) o > 70 (Sobrecompra).
    2. Alta Volatilidad: Mayor cambio % absoluto si no hay RSI extremo.
    """
    print(f"🔍 Iniciando escaneo de mercado: {len(MONEDAS_ANALISIS)} activos...")
    
    # --- FILTRO ANTI-REPETICIÓN ---
    historial = cargar_historial()
    ahora = time.time()
    # Filtramos monedas usadas en las últimas 16 horas para forzar variedad
    monedas_filtradas = [
        m for m in MONEDAS_ANALISIS 
        if (ahora - historial.get(m.replace("USDT", ""), 0)) > (16 * 3600)
    ]
    
    if not monedas_filtradas:
        print("⚠️ Todas las monedas son recientes. Usando lista completa para asegurar publicación.")
        monedas_filtradas = MONEDAS_ANALISIS
    else:
        print(f"ℹ️ Lista filtrada: {len(monedas_filtradas)} monedas candidatas (se ocultaron {len(MONEDAS_ANALISIS)-len(monedas_filtradas)} recientes).")

    candidatos = []

    # Endpoints de respaldo y Headers para evitar bloqueos
    endpoints = [
        "https://api.binance.us/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
        "https://api1.binance.com/api/v3/ticker/24hr",
        "https://api2.binance.com/api/v3/ticker/24hr"
    ]

    # --- 0. OBTENER CONTEXTO GLOBAL (BITCOIN) ---
    contexto_btc = 0
    try:
        btc_resp = sesion_http.get("https://api.binance.com/api/v3/ticker/24hr", params={'symbol': 'BTCUSDT'}, timeout=10)
        if btc_resp.status_code == 200:
            contexto_btc = float(btc_resp.json()['priceChangePercent'])
        print(f"🌍 Contexto Global (Bitcoin): {contexto_btc}%")
    except Exception as e:
        print("⚠️ No se pudo obtener el contexto de Bitcoin.")

    for symbol in monedas_filtradas:
        ticker = None
        base_url = "https://api.binance.com"

        for url_ticker in endpoints:
            try:
                # 1. Obtener Datos de Precio 24h con rotación
                resp = sesion_http.get(url_ticker, params={'symbol': symbol}, timeout=15)
                if resp.status_code == 200:
                    ticker = resp.json()
                    base_url = url_ticker.split("/api")[0]
                    break # Éxito, salimos del bucle de endpoints
                elif resp.status_code == 451:
                    print(f"⚠️ Bloqueo regional detectado en {url_ticker}, saltando a API secundaria...")
                else:
                    print(f"⚠️ Error {resp.status_code} conectando a {url_ticker}")
            except Exception as e:
                print(f"⚠️ Error de conexión en {url_ticker}: {e}")
                continue

        if not ticker:
            # Si Binance falla completamente, intentamos CoinGecko
            print(f"⚠️ Binance bloqueado para {symbol}. Intentando CoinGecko...")
            ticker = obtener_datos_coingecko(symbol)
            if ticker:
                base_url = None # Indicamos que no hay API de Binance para RSI
            else:
                print(f"❌ No se pudo obtener datos para {symbol} en ninguna fuente.")
                continue

        try:
            # 2. Calcular RSI 1h
            if base_url:
                rsi, ema50, _ = calcular_indicadores(symbol, base_url=base_url)
            else:
                rsi = 50 # RSI Neutro si usamos CoinGecko (solo estrategia de volatilidad)
                ema50 = None
            
            # Si falla el RSI (None) pero tenemos precio, usamos 50 para no descartar la moneda
            if rsi is None: 
                rsi = 50
                ema50 = None

            candidatos.append({
                "symbol": symbol.replace("USDT", ""),
                "lastPrice": float(ticker['lastPrice']),
                "percent": float(ticker['priceChangePercent']),
                "rsi": rsi,
                "ema50": ema50,
                "btc_change": contexto_btc
            })
            time.sleep(0.1) # Pausa cortés a la API
        except Exception as e:
            print(f"⚠️ Error analizando {symbol}: {e}")
            continue

    if not candidatos:
        print("❌ No se obtuvieron datos de mercado.")
        return None

    # CRITERIO DE SELECCIÓN
    # Filtramos RSI extremos (<30 o >70)
    extremos_rsi = [c for c in candidatos if c['rsi'] <= 30 or c['rsi'] >= 70]

    if extremos_rsi:
        # Si hay extremos, ganan. Ordenamos por qué tan lejos están de 50 (cuanto más lejos, más extremo)
        ganador = sorted(extremos_rsi, key=lambda x: abs(x['rsi'] - 50), reverse=True)[0]
        print(f"🏆 Ganador por RSI Extremo: {ganador['symbol']} (RSI: {ganador['rsi']:.1f})")
    else:
        # Si no, gana la que tenga mayor movimiento porcentual absoluto (subida o bajada fuerte)
        ganador = sorted(candidatos, key=lambda x: abs(x['percent']), reverse=True)[0]
        print(f"🏆 Ganador por Volatilidad: {ganador['symbol']} ({ganador['percent']}%)")

    return ganador

def generar_post_inteligente(datos_mercado):
    """
    2. Generación de Contenido con IA (Groq/Llama3):
    Redacta un análisis técnico breve y profesional.
    """
    moneda = datos_mercado['symbol']
    # Formateo a 2 decimales para la variación porcentual (Ej: 0.336 -> 0.34)
    cambio = f"{float(datos_mercado['percent']):.2f}"
    rsi = datos_mercado.get('rsi', 50)
    
    # Formateo visual más seguro para evitar precios en '0' o vacíos
    precio_float = float(datos_mercado['lastPrice'])
    if precio_float < 0.0001:
        # Para memecoins con muchos ceros (ej. PEPE, SHIB)
        precio = f"{precio_float:.8f}".rstrip("0").rstrip(".")
    elif precio_float < 1:
        # Para monedas menores a 1$ pero sin tantos ceros (ej. TRX, ADA)
        precio = f"{precio_float:.5f}".rstrip("0").rstrip(".")
    else:
        # Para monedas como BTC, ETH, SOL
        precio = f"{precio_float:.2f}"
    if not precio: precio = "0"

    # Contexto dinámico para que la IA tenga variedad en su análisis
    cambio_float = float(cambio)
    if cambio_float > 20:
        contexto_tecnico = "Subida explosiva (posible FOMO). Tendencia fuertemente alcista. Menciona cautela por volatilidad."
    elif cambio_float > 5:
        contexto_tecnico = "Tendencia alcista sólida. El activo está ganando valor. Menciona fortaleza."
    elif cambio_float < -20:
        contexto_tecnico = "Caída severa (posible capitulación o pánico). Tendencia fuertemente bajista. Analiza si es oportunidad de rebote o riesgo."
    elif cambio_float < -5:
        contexto_tecnico = "Tendencia bajista continua. El activo ha estado perdiendo valor de forma constante. Sugiere precaución."
    else:
        contexto_tecnico = "Mercado lateral o consolidando. Volatilidad moderada, tendencia neutra."
    
    estado_rsi = "Neutro"
    if rsi > 70: estado_rsi = "Sobrecompra (Riesgo de corrección)"
    elif rsi < 30: estado_rsi = "Sobreventa (Oportunidad de rebote)"
    
    # Condicionar si hablamos del RSI. Si es 50 (neutro o por defecto de API caída), omitimos mencionarlo.
    if rsi != 50:
        info_tecnica = f"- RSI (1h): {rsi:.1f} ({estado_rsi})"
        instruccion_datos = f"Integra los datos ({precio}, RSI) dentro de las oraciones de forma narrativa."
    else:
        info_tecnica = "- Enfoque: Acción del precio, volatilidad y tendencia reciente."
        instruccion_datos = f"Integra el precio ({precio}) y la variación dentro de las oraciones. NO menciones el RSI en absoluto."

    fomo = datos_mercado.get('fomo')
    if fomo:
        info_tecnica += f"\n    - Métrica de Sentimiento: {fomo}% de usuarios alcistas."
        instruccion_datos += " Interpreta el sentimiento de la comunidad con tus propias palabras, relaciona el nivel de optimismo/miedo con los fundamentales del proyecto."

    ema50 = datos_mercado.get('ema50')
    if ema50:
        tendencia_ema = "Alcista (Precio > EMA50)" if precio_float > ema50 else "Bajista o Falso Rebote (Precio < EMA50)"
        info_tecnica += f"\n    - Media Móvil (EMA 50): {tendencia_ema}."

    btc_change = datos_mercado.get('btc_change', 0)
    if btc_change <= -2:
        info_tecnica += f"\n    - Efecto Bitcoin: ⚠️ BTC está cayendo ({btc_change}%), advierte que podría arrastrar a esta moneda."
    elif btc_change >= 2:
        info_tecnica += f"\n    - Efecto Bitcoin: 🔥 BTC subiendo ({btc_change}%), viento a favor para el mercado general."

    # --- VARIACIÓN ALEATORIA DE ESTRUCTURA Y TONO (ANTI-REPETICIÓN) ---
    enfoques = [
        "Empieza directamente con un dato histórico fascinante o curiosidad sobre la moneda, y luego conecta eso con la variación de precio actual.",
        "Ve directo al grano con el análisis del precio y volatilidad, y luego menciona una noticia o detalle técnico del proyecto que respalde el movimiento.",
        "Inicia con una pregunta provocativa sobre el futuro del activo. Analiza el precio actual y da una píldora de conocimiento experto.",
        "Usa un tono de urgencia (alerta de tendencia). Destaca el precio primero, luego lanza un 'dato que pocos saben' sobre su tecnología."
    ]
    enfoque_seleccionado = random.choice(enfoques)

    prompt = f"""
    Actúa como un Top Influencer y Trader Experto en Binance Square con miles de seguidores.
    Tu objetivo es hacer que este post se vuelva VIRAL, atraiga likes, comentarios y muchísimos seguidores.
    
    DATOS DEL MERCADO:
    - Activo: {moneda}
    - Precio: {precio} USDT (Variación: {cambio}%)
    {info_tecnica}
    - Contexto: {contexto_tecnico}
    
    ESTILO DE REDACCIÓN (COPIA A LOS MEJORES CREADORES):
    1. 🪝 GANCHO PODEROSO: Inicia con un titular impactante en MAYÚSCULAS y emojis que detenga el scroll (Ej: "¿ESTAMOS ANTE EL DESPEGUE DE {moneda}?", "🚨 ALERTA DE MOVIMIENTO MASIVO EN {moneda}").
    2. 📝 FORMATO VISUAL RÁPIDO: NO uses párrafos densos. Usa viñetas con emojis (👉, 💡, ⚠️, 🚀) para que la información técnica sea súper fácil de leer. ¡Usa listas y mucho salto de línea!
    3. 🧠 FUNDAMENTO SIMPLE: Explica en 1 o 2 viñetas breves QUÉ hace {moneda} o qué narrativa empuja su precio. Convierte el análisis técnico complejo ({instruccion_datos}) en algo simple que un principiante entienda.
    4. 💬 INTERACCIÓN (CLAVE PARA EL ALGORITMO): Termina SIEMPRE con una pregunta directa y fácil de responder (Ej: "¿Tienes {moneda} en tu portafolio? Los leo 👇").
    5. 🎁 CRECIMIENTO: Pide explícitamente a los usuarios que le den a "Seguir" para recibir tus alertas tempranas y ganar dinero.
    
    REGLAS:
    - Extensión recomendada: Entre 500 y 800 caracteres. Textos escaneables.
    - Incluye al final: @BinanceES ${moneda} $BNB #{moneda} #Binance #CryptoMarket
    """
    
    return generar_texto_ia(prompt)

def obtener_fear_and_greed():
    """
    Obtiene el índice de Miedo y Codicia desde alternative.me
    """
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        print(f"📡 Consultando Fear & Greed Index...")
        response = sesion_http.get(url, timeout=10)
        data = response.json()
        if data['data']:
            return data['data'][0]
    except Exception as e:
        print(f"⚠️ Error obteniendo F&G Index: {e}")
    return None

def generar_post_fng(datos_fng):
    valor = datos_fng['value']
    clasificacion = datos_fng['value_classification']
    
    prompt = f"""
    Actúa como un 'Top Creator' de Binance Square.
    DATOS: Índice de Miedo y Codicia (Fear & Greed): {valor}/100 ({clasificacion}).
    
    OBJETIVO: Post diario de sentimiento de mercado (Viral).
    
    ESTILO:
    - 🪝 TITULAR VIRAL: Relaciona el sentimiento con la acción de las ballenas en mayúsculas (Ej: "🐳 ¿QUÉ ESTÁN HACIENDO LAS BALLENAS HOY?").
    - 📊 EL DATO CLAVE: Destaca que estamos en {valor}/100 ({clasificacion}) usando una viñeta clara.
    - 🧠 PSICOLOGÍA DEL MERCADO: Explica en una o dos líneas qué significa esto para el inversor común (Ej: "El miedo extremo es donde se hacen las fortunas" o "Codicia extrema = cuidado con las correcciones").
    - 💬 PREGUNTA A LA COMUNIDAD: Invita a comentar: "¿Tú estás comprando, vendiendo o holdeando? Te leo en los comentarios 👇".
    - ➕ FOLLOW: "Dale a SEGUIR para tu actualización diaria del mercado."
    
    REGLAS:
    - Muy visual, usa emojis y espacios. Párrafos cortísimos.
    - Extensión: Unos 350 - 500 caracteres.
    - OBLIGATORIO: Hashtags #Bitcoin #FearAndGreed #Binance
    """
    
    return generar_texto_ia(prompt)

def calcular_indicadores(symbol, period_rsi=14, period_ema=50, base_url="https://api.binance.com", headers=None):
    """
    Calcula el RSI (1h) y la EMA 50 para confirmar tendencia.
    """
    try:
        url = f"{base_url}/api/v3/klines"
        # Traemos 100 velas de 1h para calcular bien el promedio
        params = {'symbol': symbol, 'interval': '1h', 'limit': 100}
        response = sesion_http.get(url, params=params, timeout=10)
        data = response.json()
        
        if not data or len(data) < max(period_rsi, period_ema) + 1:
            return None, None, None

        closes = [float(x[4]) for x in data]
        
        # Cálculo manual de RSI
        gains = []
        losses = []
        
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i-1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
            
        # Promedio inicial
        avg_gain = sum(gains[:period_rsi]) / period_rsi
        avg_loss = sum(losses[:period_rsi]) / period_rsi
        
        # Suavizado (Wilder's Smoothing)
        for i in range(period_rsi, len(gains)):
            avg_gain = (avg_gain * (period_rsi - 1) + gains[i]) / period_rsi
            avg_loss = (avg_loss * (period_rsi - 1) + losses[i]) / period_rsi
            
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            
        # Cálculo de la EMA 50
        sma = sum(closes[:period_ema]) / period_ema
        ema = sma
        multiplier = 2 / (period_ema + 1)
        for close in closes[period_ema:]:
            ema = (close - ema) * multiplier + ema

        return rsi, ema, closes[-1]
    except Exception as e:
        # print(f"⚠️ Debug RSI {symbol}: {e}") # Descomentar para depuración profunda
        return None, None, None

def generar_post_rsi(datos):
    moneda = datos['symbol']
    rsi = int(datos['rsi']) if datos['rsi'] else 50
    
    precio_float = float(datos['price'])
    if precio_float < 0.0001:
        precio = f"{precio_float:.8f}".rstrip("0").rstrip(".")
    elif precio_float < 1:
        precio = f"{precio_float:.5f}".rstrip("0").rstrip(".")
    else:
        precio = f"{precio_float:.2f}"
    if not precio: precio = "0"
    
    fomo = datos.get('fomo')
    contexto_fomo = ""
    if fomo:
        contexto_fomo = f"\n    - Sentimiento social actual: {fomo}% de usuarios alcistas."

    ema50 = datos.get('ema50')
    if ema50:
        tendencia_ema = "Soportada sobre la EMA 50 (Fuerte)" if precio_float > ema50 else "Debajo de la EMA 50 (Riesgo de falsa subida)"
        contexto_fomo += f"\n    - EMA 50: {tendencia_ema}."
        
    btc_change = datos.get('btc_change', 0)
    if btc_change <= -2:
        contexto_fomo += f"\n    - ALERTA MACRO: Bitcoin está sangrando ({btc_change}%)."
    elif btc_change >= 2:
        contexto_fomo += f"\n    - ALERTA MACRO: Bitcoin está liderando el mercado ({btc_change}%)."

    if rsi <= 30:
        estado = "SOBREVENTA (Oversold)"
        objetivo = 'alerta de oportunidad ("Buy the Dip" / posible rebote inminente)'
        explicacion_rsi = f"el RSI de {rsi}/100 indica agotamiento de vendedores o zona de acumulación"
        hashtag = "#BuyTheDip"
    else:
        estado = "SOBRECOMPRA (Overbought)"
        objetivo = 'alerta de precaución (posible corrección o toma de ganancias inminente)'
        explicacion_rsi = f"el RSI de {rsi}/100 indica euforia en el mercado, posible techo local o agotamiento de compradores"
        hashtag = "#TakeProfit"

    prompt = f"""
    Actúa como un Top Trader de Binance Square que da las mejores "señales" y análisis en tiempo real.
    DATOS: {moneda} está en zona de {estado} en gráfico de 1h. Precio: {precio}. {contexto_fomo}
    
    OBJETIVO: Crear una {objetivo} que suene ÚNICA y humana.
    
    ESTILO DE CREADOR EXITOSO (ALTO ALCANCE):
    1. 🪝 TITULAR DE IMPACTO: Empieza con mayúsculas y emojis de alerta máxima (Ej: "🚨 ¡CUIDADO CON {moneda}!" o "🚀 SEÑAL DE OPORTUNIDAD EN {moneda}").
    2. 📝 ESCANEO RÁPIDO: Usa viñetas o checkmarks (✅, ❌, 📊) para explicar de forma sencilla por qué {explicacion_rsi}.
    3. 🧠 FUNDAMENTO: Menciona brevemente el sector del token para justificar el movimiento, pero mantén el enfoque en la acción del precio.
    4. 💥 ENGAGEMENT: Haz una pregunta polémica o muy directa (Ej: "¿Compras el dip o esperas más caída? 👇").
    5. 🚀 FOLLOW URGE: "Dale click en SEGUIR para no perderte mi próxima alerta en tiempo real."
    
    REGLAS:
    - Formato visual escaneable (espacios en blanco, no bloques de texto de más de 2 líneas juntas).
    - OBLIGATORIO: Cashtags ${moneda} {hashtag} #Binance #CryptoMarket
    """
    
    return generar_texto_ia(prompt)

def generar_imagen_crypto(moneda, sentimiento="bullish", filename="crypto_post.jpg"):
    """Genera una imagen 3D atractiva de la criptomoneda usando Pollinations.ai"""
    print(f"🎨 Generando imagen visual para {moneda} ({sentimiento})...")
    
    adjetivo = "rocket taking off, bullish, glowing green neon lights" if sentimiento == "bullish" else "bear market, warning red neon lights, dramatic shadows"
    prompt = f"3D render of {moneda} cryptocurrency physical coin, {adjetivo}, photorealistic, 8k resolution, cinematic lighting, trading background"
    encoded_prompt = urllib.parse.quote(prompt)
    
    seed = random.randint(1, 1000000)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"
    
    try:
        response = sesion_http.get(image_url, timeout=30)
        response.raise_for_status()
        with open(filename, 'wb') as f:
            f.write(response.content)
        print(f"✅ Imagen generada y guardada como {filename}")
        return filename
    except Exception as e:
        print(f"⚠️ Error generando imagen con IA: {e}")
        return None

if __name__ == "__main__":
    print("🤖 Iniciando Bot vIcmAr...")
    print(f"⚙️ Versión 2.1 - Modelo: {GROQ_MODEL_NAME} | Modo: {TIPO_BOT}")
    
    if TIPO_BOT == "FNG":
        # --- MODO FEAR & GREED ---
        datos = obtener_fear_and_greed()
        if datos:
            print(f"🌡️ Índice obtenido: {datos['value']} ({datos['value_classification']})")
            post = generar_post_fng(datos)
            if post:
                publicar_en_square(post)
                # Nota: No guardamos historial para F&G porque es un post diario único.
    
    else:
        # --- MODO REPORTE DIARIO (Matutino/Vespertino) ---
        
        # 1. Determinar Saludo según Horario UTC
        hora_actual = datetime.now(timezone.utc).hour
        saludo_telegram = "🤖 Reporte vIcmAr"
        
        # 09:30 UTC es mañana AR / 22:00 UTC es noche AR
        if 8 <= hora_actual <= 11:
            saludo_telegram = "🌅 Reporte Matutino vIcmAr"
        elif 20 <= hora_actual <= 23:
            saludo_telegram = "🌆 Reporte Vespertino vIcmAr"
            
        # 2. Obtener la Mejor Oportunidad de la Sesión
        oportunidad = analizar_oportunidades()
        
        if oportunidad:
            # Adaptamos datos para consistencia (lastPrice -> price en funciones viejas)
            oportunidad['price'] = oportunidad['lastPrice'] 
            
            # NUEVO: Obtener FOMO de la comunidad para la moneda ganadora
            print(f"👥 Obteniendo sentimiento social (FOMO) para {oportunidad['symbol']}...")
            oportunidad['fomo'] = obtener_fomo_coingecko(oportunidad['symbol'] + "USDT")
            
            # Generamos Post Corto para Square (usamos la lógica inteligente general o RSI si es extremo)
            if oportunidad['rsi'] <= 30 or oportunidad['rsi'] >= 70:
                post_square = generar_post_rsi(oportunidad)
                sentimiento = "bullish" if oportunidad['rsi'] <= 30 else "bearish"
            else:
                post_square = generar_post_inteligente(oportunidad)
                sentimiento = "bullish" if float(oportunidad['percent']) > 0 else "bearish"
                
            # Generar imagen atractiva
            imagen_path = generar_imagen_crypto(oportunidad['symbol'], sentimiento)
            
            # Publicar en Square (AQUÍ PASAMOS LA IMAGEN A LA FUNCIÓN)
            if post_square and publicar_en_square(post_square, image_path=imagen_path):
                print(f"✅ Publicado en Square.")
                guardar_historial(oportunidad['symbol'])

                # Notificación simple a Telegram sobre la publicación en Square
                enviar_telegram(f"✅ Publicado nuevo análisis de {oportunidad['symbol']} en Binance Square.")
                
                # Limpieza del archivo temporal
                if imagen_path and os.path.exists(imagen_path):
                    os.remove(imagen_path)

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from pdf2image import convert_from_bytes
import base64
import io
from openai import OpenAI
import requests
import re
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# CORS configurado para producción
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

def get_openai_client():
    """Obtener cliente OpenAI configurado"""
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise Exception('OPENAI_API_KEY no configurada')

    # Asignar la KEY en el entorno (requerido para OpenAI v1.x)
    os.environ["OPENAI_API_KEY"] = api_key

    # Crear cliente sin parámetros (la librería toma la key desde el entorno)
    return OpenAI()


def extract_cedula_from_filename(filename):
    """Extraer cédula (10 dígitos) del nombre del archivo"""
    match = re.search(r'(\d{10})', filename)
    return match.group(1) if match else None

def get_cedula_info(cedula):
    """Consultar datos de cédula en registro civil de Ecuador"""
    try:
        response = requests.post(
            'https://si.secap.gob.ec/sisecap/logeo_web/json/busca_persona_registro_civil.php',
            data={'documento': cedula, 'tipo': 1},
            timeout=10  # Aumentado a 10 segundos
        )
        
        # Verificar que la respuesta sea exitosa
        if response.status_code != 200:
            print(f'  ⚠️  API cédula respondió con código: {response.status_code}')
            return None
            
        data = response.json()
        
        # Verificar que tenga datos válidos
        if not data or not data.get('nombres'):
            print(f'  ⚠️  API cédula no retornó datos válidos')
            return None
            
        return data
        
    except requests.exceptions.Timeout:
        print(f'  ⚠️  Timeout consultando API de cédula (>10s)')
        return None
    except requests.exceptions.RequestException as e:
        print(f'  ⚠️  Error de red consultando cédula: {str(e)}')
        return None
    except json.JSONDecodeError as e:
        print(f'  ⚠️  Respuesta de API cédula no es JSON válido: {str(e)}')
        return None
    except Exception as e:
        print(f'  ⚠️  Error inesperado consultando cédula: {str(e)}')
        return None

def convert_pdf_to_images(pdf_bytes):
    """Convertir PDF a imágenes PNG en base64"""
    try:
        print('  📄 Convirtiendo PDF a imágenes...')
        
        # Convertir PDF a imágenes (DPI 200 para buena calidad)
        images = convert_from_bytes(
            pdf_bytes, 
            dpi=200, 
            fmt='png',
            thread_count=2  # Optimización para Render
        )
        
        base64_images = []
        max_pages = min(len(images), 5)  # Máximo 5 páginas
        
        for i in range(max_pages):
            # Convertir imagen a base64
            buffered = io.BytesIO()
            images[i].save(buffered, format="PNG", optimize=True)
            img_base64 = base64.b64encode(buffered.getvalue()).decode()
            base64_images.append(img_base64)
            print(f'  ✅ Página {i + 1} convertida')
        
        if not base64_images:
            raise Exception('No se pudieron extraer imágenes del PDF')
        
        return base64_images
        
    except Exception as e:
        print(f'  ❌ Error convirtiendo PDF: {str(e)}')
        raise

def process_pdf(pdf_bytes, filename):
    """Procesar PDF completo: extraer cédula, convertir a imágenes y analizar con IA"""
    
    # Extraer cédula del nombre del archivo (opcional)
    cedula = extract_cedula_from_filename(filename)
    cedula_info = None
    
    if cedula:
        print(f'  ✓ Cédula encontrada: {cedula}')
        try:
            cedula_info = get_cedula_info(cedula)
            if cedula_info and cedula_info.get('nombres'):
                print(f'  ✓ Datos obtenidos: {cedula_info.get("nombres")} {cedula_info.get("apellidos")}')
            else:
                print(f'  ⚠️  No se encontraron datos para cédula: {cedula}')
        except Exception as e:
            print(f'  ⚠️  Error consultando cédula (continuando sin datos): {str(e)}')
    else:
        print(f'  ℹ️  No se encontró cédula en el nombre del archivo')
    
    # Convertir PDF a imágenes (esto es crítico, si falla aquí sí debe parar)
    try:
        images = convert_pdf_to_images(pdf_bytes)
    except Exception as e:
        print(f'  ❌ Error crítico convirtiendo PDF: {str(e)}')
        raise Exception(f'No se pudo convertir el PDF a imágenes: {str(e)}')
    
    print(f'  🔄 Analizando {len(images)} página(s) con OpenAI GPT-4 Vision...')
    
    # Prompt optimizado para extracción de certificados médicos
    content = [
        {
            "type": "text",
            "text": """Eres un experto extrayendo datos de certificados médicos ocupacionales escaneados.

EXTRAE EXACTAMENTE estos campos:

1. **aptitudMedica**: En sección "APTITUD MÉDICA" o similar. Valores posibles: APTO / APTO EN OBSERVACIÓN / APTO CON LIMITACIONES / NO APTO

2. **diagnostico1**: En sección "DIAGNÓSTICO" o "K. DIAGNÓSTICO", línea 1, descripción completa

3. **cie10_diagnostico1**: Código CIE-10 del diagnóstico 1 - SOLO el código (ej: I089, H521)

4. **observaciones1**: Observaciones del diagnóstico 1. Busca en "Observación", "Limitación", o "RECOMENDACIONES"

5. **diagnostico2**: Segundo diagnóstico si existe

6. **cie10_diagnostico2**: Código CIE-10 del diagnóstico 2

7. **observaciones2**: Observaciones del diagnóstico 2

8. **hallazgoMetabolico**: En "RESULTADOS EXÁMENES" busca valores metabólicos (glucosa, triglicéridos, colesterol). Incluye valor numérico

9. **hallazgoOsteomuscular**: En "EXAMEN FÍSICO" o resultados de Rx busca problemas de columna/articulaciones

10. **otrosAntecedentes**: En "ANTECEDENTES PERSONALES" lista cirugías y alergias

REGLAS IMPORTANTES:
- Copia el texto EXACTO del documento
- Para CIE-10: SOLO el código, sin prefijos (correcto: "I089", incorrecto: "CIE-10: I089")
- Si un campo no existe, usa: "No especificado"
- NO inventes datos
- El documento puede estar escaneado o con mala calidad, haz tu mejor esfuerzo

Responde SOLO con este JSON:
{
  "aptitudMedica": "...",
  "diagnostico1": "...",
  "cie10_diagnostico1": "...",
  "observaciones1": "...",
  "diagnostico2": "...",
  "cie10_diagnostico2": "...",
  "observaciones2": "...",
  "hallazgoMetabolico": "...",
  "hallazgoOsteomuscular": "...",
  "otrosAntecedentes": "..."
}"""
        }
    ]
    
    # Agregar todas las imágenes al prompt
    for img_base64 in images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{img_base64}",
                "detail": "high"  # Máxima calidad para PDFs escaneados
            }
        })
    
    # Llamar a OpenAI GPT-4 Vision
    client = get_openai_client()
    
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2500,
        temperature=0.1,  # Baja temperatura para precisión
        messages=[{"role": "user", "content": content}]
    )
    
    # Datos por defecto
    extracted_data = {
        'aptitudMedica': 'No especificado',
        'diagnostico1': 'No especificado',
        'cie10_diagnostico1': 'No especificado',
        'observaciones1': 'No especificado',
        'diagnostico2': 'No especificado',
        'cie10_diagnostico2': 'No especificado',
        'observaciones2': 'No especificado',
        'hallazgoMetabolico': 'No especificado',
        'hallazgoOsteomuscular': 'No especificado',
        'otrosAntecedentes': 'No especificado',
    }
    
    # Parsear respuesta de OpenAI
    try:
        respuesta = response.choices[0].message.content
        print(f'  📊 Respuesta recibida: {respuesta[:100]}...')
        
        # Limpiar markdown del JSON
        json_str = respuesta.strip()
        json_str = json_str.replace('```json', '').replace('```', '').strip()
        
        # Parsear JSON
        parsed = json.loads(json_str)
        extracted_data.update(parsed)
        print('  ✅ Datos extraídos correctamente')
        
    except Exception as e:
        print(f'  ⚠️  Error parseando JSON: {str(e)}')
        print(f'  Respuesta original: {respuesta}')
    
    # Preparar datos de retorno con valores seguros
    nombre = cedula_info.get('nombres', 'Sin datos') if cedula_info else 'Sin datos'
    apellido = cedula_info.get('apellidos', 'Sin datos') if cedula_info else 'Sin datos'
    
    # Retornar datos completos
    return {
        'fileName': filename,
        'cedula': cedula if cedula else 'No detectada',
        'nombre': nombre,
        'apellido': apellido,
        **extracted_data
    }

@app.route('/api/process-clinical-history', methods=['POST', 'OPTIONS'])
def process_clinical_history():
    """Endpoint principal para procesar PDFs"""
    
    # Manejar preflight CORS
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    
    try:
        print('\n' + '='*50)
        print('🔥 Nueva petición de procesamiento')
        print('='*50)
        
        # Validar que vengan archivos
        if 'files' not in request.files:
            print('❌ No se recibieron archivos')
            return jsonify({
                'success': False, 
                'procesados': 0, 
                'errores': 1, 
                'data': [],
                'mensaje': 'No se recibieron archivos'
            }), 400
        
        files = request.files.getlist('files')
        
        if not files or len(files) == 0:
            print('❌ Lista de archivos vacía')
            return jsonify({
                'success': False, 
                'procesados': 0, 
                'errores': 1, 
                'data': [],
                'mensaje': 'Lista de archivos vacía'
            }), 400
        
        resultados = []
        errores = []
        
        print(f'📄 Procesando {len(files)} archivo(s)...\n')
        
        # Procesar cada archivo
        for idx, file in enumerate(files, 1):
            try:
                print(f'[{idx}/{len(files)}] ⏳ Procesando: {file.filename}')
                
                # Leer bytes del PDF
                pdf_bytes = file.read()
                
                if len(pdf_bytes) == 0:
                    raise Exception('Archivo vacío')
                
                # Procesar PDF
                datos = process_pdf(pdf_bytes, file.filename)
                resultados.append(datos)
                
                print(f'[{idx}/{len(files)}] ✅ Completado: {file.filename}\n')
                
            except Exception as error:
                error_msg = str(error)
                errores.append({
                    'archivo': file.filename, 
                    'error': error_msg
                })
                print(f'[{idx}/{len(files)}] ❌ Error en {file.filename}: {error_msg}\n')
        
        print('='*50)
        print(f'✅ Procesados: {len(resultados)} | ❌ Errores: {len(errores)}')
        print('='*50 + '\n')
        
        return jsonify({
            'success': len(resultados) > 0,
            'procesados': len(resultados),
            'errores': len(errores),
            'data': resultados,
            'errores_detalle': errores if errores else None
        }), 200
    
    except Exception as error:
        print(f'❌ Error general: {str(error)}')
        return jsonify({
            'success': False, 
            'procesados': 0, 
            'errores': 1, 
            'data': [],
            'mensaje': f'Error del servidor: {str(error)}'
        }), 500

@app.route('/', methods=['GET'])
def index():
    """Endpoint de health check"""
    return jsonify({
        "status": "ok",
        "service": "API Procesador de Certificados Médicos",
        "version": "1.0.0",
        "endpoints": {
            "POST /api/process-clinical-history": "Procesar certificados médicos en PDF"
        }
    }), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check para Render"""
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    
    print("""
╔═══════════════════════════════════════════════╗
║   Procesador de Certificados Médicos         ║
║   PDF Escaneados → Imágenes → GPT-4 Vision   ║
╚═══════════════════════════════════════════════╝

🌐 Servidor: http://0.0.0.0:{port}
🔑 OpenAI: {'✅ Configurado' if os.getenv('OPENAI_API_KEY') else '❌ NO CONFIGURADO'}
🚀 Modo: {'Development' if debug_mode else 'Production'}
    """.format(port=port))
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
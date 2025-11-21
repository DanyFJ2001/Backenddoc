from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
import pymupdf4llm   # ✔ reemplazo de PyMuPDF que sí funciona en Render
from PIL import Image
import io
from openai import OpenAI
import requests
import re
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

def extract_cedula_from_filename(filename):
    match = re.search(r'(\d{10})', filename)
    return match.group(1) if match else None

def get_cedula_info(cedula):
    try:
        response = requests.post(
            'https://si.secap.gob.ec/sisecap/logeo_web/json/busca_persona_registro_civil.php',
            data={'documento': cedula, 'tipo': 1},
            timeout=10
        )
        if response.status_code != 200:
            return None
        data = response.json()
        if not data or not data.get('nombres'):
            return None
        return data
    except:
        return None

# ========================
#   🔥 NUEVA FUNCIÓN
#   PDF → IMÁGENES usando pymupdf4llm
# ========================
def convert_pdf_to_images(pdf_bytes):
    try:
        print('  📄 Convirtiendo PDF a imágenes (Render Safe)...')

        pixmaps = pymupdf4llm.get_pixmaps(pdf_bytes)
        base64_images = []

        max_pages = min(len(pixmaps), 5)

        for i in range(max_pages):
            pix = pixmaps[i]

            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            img_base64 = base64.b64encode(img_bytes.getvalue()).decode()
            base64_images.append(img_base64)

            print(f'  ✅ Página {i+1} convertida')

        if not base64_images:
            raise Exception('No se pudieron extraer imágenes del PDF')

        return base64_images

    except Exception as e:
        print(f'  ❌ Error convirtiendo: {str(e)}')
        raise


def process_pdf(pdf_bytes, filename):
    cedula = extract_cedula_from_filename(filename)
    cedula_info = None

    if cedula:
        print(f'  ✓ Cédula: {cedula}')
        cedula_info = get_cedula_info(cedula)
        if cedula_info:
            print(f'  ✓ Datos: {cedula_info.get("nombres")} {cedula_info.get("apellidos")}')

    images = convert_pdf_to_images(pdf_bytes)

    print(f'  🔄 Analizando {len(images)} página(s) con OpenAI Vision...')

    content = [
        {
            "type": "text",
            "text": """Eres un experto extrayendo datos de certificados médicos ocupacionales.

EXTRAE EXACTAMENTE:

1. aptitudMedica
2. diagnostico1
3. cie10_diagnostico1
4. observaciones1
5. diagnostico2
6. cie10_diagnostico2
7. observaciones2
8. hallazgoMetabolico
9. hallazgoOsteomuscular
10. otrosAntecedentes

REGLAS:
- Copia texto EXACTO del documento
- CIE-10: SOLO código
- Incluye valores numéricos
- Si no existe → "No especificado"
- NO inventes datos
JSON:
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

    for img_base64 in images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{img_base64}",
                "detail": "high"
            }
        })

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2500,
        temperature=0.1,
        messages=[{"role": "user", "content": content}]
    )

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

    try:
        respuesta = response.choices[0].message.content
        print(f'  📊 Respuesta: {respuesta[:100]}...')

        json_str = respuesta.strip()
        json_str = json_str.replace('```json', '').replace('```', '').strip()

        parsed = json.loads(json_str)
        extracted_data.update(parsed)
        print('  ✅ JSON parseado correctamente')
    except Exception as e:
        print(f'  ⚠️  Error al parsear JSON: {str(e)}')

    return {
        'fileName': filename,
        'cedula': cedula or 'No detectada',
        'nombre': cedula_info.get('nombres', 'Sin datos') if cedula_info else 'Sin datos',
        'apellido': cedula_info.get('apellidos', 'Sin datos') if cedula_info else 'Sin datos',
        **extracted_data
    }


@app.route('/api/process-clinical-history', methods=['POST'])
def process_clinical_history():
    try:
        print('\n🔥 Nueva petición')

        if 'files' not in request.files:
            return jsonify({'success': False, 'procesados': 0, 'errores': 1, 'data': []})

        files = request.files.getlist('files')

        if not files:
            return jsonify({'success': False, 'procesados': 0, 'errores': 1, 'data': []})

        resultados = []
        errores = []

        print(f'📄 {len(files)} archivo(s)')

        for file in files:
            try:
                print(f'\n  ⏳ {file.filename}')
                pdf_bytes = file.read()
                datos = process_pdf(pdf_bytes, file.filename)
                resultados.append(datos)
                print(f'  ✅ OK')
            except Exception as error:
                errores.append({'archivo': file.filename, 'error': str(error)})
                print(f'  ❌ {str(error)}')

        print(f'\n✅ {len(resultados)} procesado(s) | ❌ {len(errores)} error(es)\n')

        return jsonify({
            'success': True,
            'procesados': len(resultados),
            'errores': len(errores),
            'data': resultados,
            'errores_detalle': errores if errores else None
        })

    except Exception as error:
        print(f'❌ Error general: {str(error)}')
        return jsonify({'success': False, 'procesados': 0, 'errores': 1, 'data': []})


@app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "service": "API Procesador de Certificados Médicos",
        "version": "1.0.0"
    })


@app.route('/health')
def health():
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    print(f"""
╔═══════════════════════════════════════════════╗
║   Procesador de Historias Clínicas           ║
║      ✅ PDF → Imágenes → GPT-4 Vision        ║
╚═══════════════════════════════════════════════╝

🌐 http://0.0.0.0:{port}
""")
    app.run(host='0.0.0.0', port=port, debug=False)

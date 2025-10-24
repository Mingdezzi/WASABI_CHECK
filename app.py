import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import io
import os
import re

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func, text, Integer, String
# (*** 1. joinedload 추가 ***)
from sqlalchemy.orm import joinedload

from apscheduler.schedulers.background import BackgroundScheduler

# Google Cloud Vision 라이브러리
from google.cloud import vision
from google.oauth2 import service_account

app = Flask(__name__)

# --- DB 설정 ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(app.root_path, 'database.db')
)
app.config['SECRET_KEY'] = 'wasabi-check-secret-key'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = '/tmp'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)

IMAGE_URL_PREFIX = 'https://files.ebizway.co.kr/files/10249/Style/'

# Google Cloud 인증 정보 경로 설정
GCP_CREDENTIALS_PATH = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
vision_client = None
# ... (Google Vision Client 초기화 코드 - 이전과 동일) ...
if GCP_CREDENTIALS_PATH and os.path.exists(GCP_CREDENTIALS_PATH):
    try: credentials = service_account.Credentials.from_service_account_file(GCP_CREDENTIALS_PATH); vision_client = vision.ImageAnnotatorClient(credentials=credentials); print("Google Cloud Vision Client initialized successfully.")
    except Exception as e: print(f"Error initializing Google Cloud Vision Client: {e}")
else:
    print("GOOGLE_APPLICATION_CREDENTIALS not set or invalid.")
    local_key_path = 'gcp_credentials.json';
    if os.path.exists(local_key_path):
        try: credentials = service_account.Credentials.from_service_account_file(local_key_path); vision_client = vision.ImageAnnotatorClient(credentials=credentials); print("Initialized local Google Vision Client.")
        except Exception as e: print(f"Error initializing local Google Vision Client: {e}")

# --- DB 모델 정의 ---
class Product(db.Model):
    __tablename__ = 'products'
    product_number = db.Column(String, primary_key=True)
    product_name = db.Column(String, nullable=False)
    is_favorite = db.Column(Integer, default=0)
    release_year = db.Column(Integer)
    item_category = db.Column(String)
    variants = db.relationship('Variant', backref='product', lazy=True, cascade="all, delete-orphan")

class Variant(db.Model):
    __tablename__ = 'variants'
    barcode = db.Column(String, primary_key=True)
    product_number = db.Column(String, db.ForeignKey('products.product_number'), nullable=False)
    color = db.Column(String)
    size = db.Column(String)
    store_stock = db.Column(Integer, default=0)
    hq_stock = db.Column(Integer, default=0)
    original_price = db.Column(Integer, default=0)
    sale_price = db.Column(Integer, default=0)

# --- DB 초기화 함수 ---
def init_db():
    with app.app_context(): db.create_all(); print("DB 테이블 초기화/검증 완료.")

# --- 엑셀 임포트 ---
@app.route('/import_excel', methods=['GET', 'POST'])
def import_excel():
    # ... (이전과 동일) ...
    if request.method == 'POST':
        if 'excel_file' not in request.files: flash('파일 선택 안됨.', 'error'); return redirect(url_for('index'))
        file = request.files['excel_file'];
        if file.filename == '': flash('파일 선택 안됨.', 'error'); return redirect(url_for('index'))
        if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            try:
                file_content = file.read(); df = pd.read_excel( io.BytesIO(file_content), sheet_name=0, dtype={'barcode': str, 'product_number': str}, keep_default_na=False )
                required_cols_base = [ 'product_number', 'product_name', 'color', 'barcode', 'size', 'release_year', 'item_category', 'original_price', 'sale_price', 'store_stock', 'hq_stock']
                excel_cols = list(df.columns);
                if not all(col in excel_cols for col in required_cols_base): missing = [col for col in required_cols_base if col not in excel_cols]; flash(f"엑셀 컬럼명 오류. 누락: {missing}", 'error'); return redirect(url_for('index'))
                if 'is_favorite' not in excel_cols: df['is_favorite'] = 0
                else: df['is_favorite'] = pd.to_numeric(df['is_favorite'], errors='coerce').fillna(0).astype(int)
                db.session.query(Variant).delete(); db.session.query(Product).delete(); db.session.commit()
                product_cols = ['product_number', 'product_name', 'release_year', 'item_category', 'is_favorite']; actual_product_cols = [col for col in product_cols if col in df.columns]
                products_df = df[actual_product_cols].drop_duplicates(subset=['product_number']).copy(); products_df.dropna(subset=['product_number'], inplace=True)
                if 'release_year' in products_df.columns: products_df['release_year'] = pd.to_numeric(products_df['release_year'], errors='coerce').astype('Int64')
                products_data = products_df.to_dict('records'); db.session.bulk_insert_mappings(Product, products_data)
                variant_cols = [ 'barcode', 'product_number', 'color', 'size', 'store_stock', 'hq_stock', 'original_price', 'sale_price' ]; actual_variant_cols = [col for col in variant_cols if col in df.columns]
                variants_df = df[actual_variant_cols].copy(); variants_df.dropna(subset=['barcode'], inplace=True)
                for col in ['store_stock', 'hq_stock', 'original_price', 'sale_price']:
                    if col in variants_df.columns: variants_df[col] = pd.to_numeric(variants_df[col], errors='coerce').fillna(0).astype(int)
                variants_data = variants_df.to_dict('records'); db.session.bulk_insert_mappings(Variant, variants_data); db.session.commit()
                flash(f"성공 ({file.filename}): {len(products_df)}개 상품, {len(variants_df)}개 SKU 임포트.", 'success')
            except Exception as e: db.session.rollback(); flash(f"임포트 오류: {e}", 'error')
            return redirect(url_for('index'))
        else: flash('엑셀 파일만 업로드 가능.', 'error'); return redirect(url_for('index'))
    return redirect(url_for('index'))

# --- 웹페이지 라우트 ---
# (*** 수정된 index 함수 ***)
@app.route('/')
def index():
    query = request.args.get('query', '')
    showing_favorites = False
    products = [] # 결과를 담을 리스트 초기화

    if query:
        # 검색 시: Product 테이블만 검색 (variants 정보 불필요)
        search_term = f'%{query}%'
        products = Product.query.filter(
            or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term))
        ).order_by(Product.product_name).all()
    else:
        # 즐겨찾기 목록: Product와 연관된 Variants 정보도 함께 로드
        showing_favorites = True
        products = Product.query.options(
            joinedload(Product.variants) # variants 정보를 join해서 한번에 불러옴
        ).filter(
            Product.is_favorite == 1
        ).order_by(Product.product_name).all()

    return render_template('index.html', products=products, query=query, showing_favorites=showing_favorites)


def get_sort_key(variant):
    # ... (이전과 동일) ...
    color = variant.color or ''; size_str = str(variant.size).upper().strip();
    if size_str == '2XS': size_str = 'XXS'; elif size_str == '2XL': size_str = 'XXL'; elif size_str == '3XL': size_str = 'XXXL'
    custom_order = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL'];
    if size_str.isdigit(): sort_key = (1, int(size_str), '')
    elif size_str in custom_order: sort_key = (2, custom_order.index(size_str), '')
    else: sort_key = (3, 0, size_str)
    return (color, sort_key)

@app.route('/product/<product_number>')
def product_detail(product_number):
    # ... (이전과 동일) ...
    product = Product.query.get(product_number);
    if product is None: flash("상품 없음.", 'error'); return redirect(url_for('index'))
    image_url = f"{IMAGE_URL_PREFIX}{product.product_number}.jpg"; variants_list = sorted(product.variants, key=get_sort_key); related_products = []
    if product.product_name:
        search_words = product.product_name.split(' ');
        if search_words: search_term = search_words[-1];
        if len(search_term) > 1: related_products = Product.query.filter( Product.product_name.ilike(f'%{search_term}%'), Product.product_number != product_number ).limit(5).all()
    return render_template( 'detail.html', product=product, image_url=image_url, variants=variants_list, related_products=related_products )

# --- API 라우트 ---
@app.route('/barcode_search', methods=['POST'])
def barcode_search():
    # ... (이전과 동일) ...
    data = request.json; barcode = data.get('barcode');
    if not barcode: return jsonify({'status': 'error', 'message': '바코드 없음.'}), 400
    scanned_clean = barcode.replace('-', '').strip();
    if len(scanned_clean) < 11: return jsonify({'status': 'error', 'message': f'바코드 짧음 ({len(scanned_clean)}자리).'}), 400
    variant = Variant.query.filter( func.replace(Variant.barcode, '-', '').startswith(scanned_clean) ).first();
    if variant: return jsonify({'status': 'success', 'product_number': variant.product_number})
    else: return jsonify({'status': 'error', 'message': 'DB에 일치하는 바코드 없음.'}), 404

@app.route('/ocr_upload', methods=['POST'])
def ocr_upload():
    # ... (이전과 동일) ...
    if vision_client is None: return jsonify({'status': 'error', 'message': 'Google Cloud Vision 클라이언트 초기화 실패.'}), 500
    if 'ocr_image' not in request.files: return jsonify({'status': 'error', 'message': '이미지 파일 없음.'}), 400
    file = request.files['ocr_image'];
    if file.filename == '': return jsonify({'status': 'error', 'message': '파일 이름 없음.'}), 400
    if file:
        try:
            content = file.read(); image = vision.Image(content=content);
            response = vision_client.text_detection(image=image); texts = response.text_annotations;
            if response.error.message: raise Exception(f'Vision API Error: {response.error.message}')
            if texts:
                ocr_text = texts[0].description; print(f"Google Vision OCR Raw Text: {ocr_text}")
                cleaned_text = ocr_text.upper().replace('\n', ' ').replace('\r', ' '); cleaned_text = re.sub(r'\s+', ' ', cleaned_text)
                product_number_pattern = r'\bM[A-Z0-9-]{4,}\b'; matches = re.findall(product_number_pattern, cleaned_text); print(f"Found Product Number Candidates: {matches}")
                if matches:
                    search_text_raw = matches[0]; cleaned_search_text = search_text_raw.replace('-', '')
                    if len(cleaned_search_text) < 5: return jsonify({'status': 'error', 'message': f'찾은 품번 패턴 "{search_text_raw}" 짧음.'}), 400
                    print(f"Searching DB with cleaned prefix: {cleaned_search_text}")
                    results = Product.query.filter( func.replace(Product.product_number, '-', '').startswith(cleaned_search_text) ).all(); print(f"Found: {len(results)}")
                    if len(results) == 1: return jsonify({'status': 'found_one', 'product_number': results[0].product_number})
                    elif len(results) > 1: return jsonify({'status': 'found_many', 'query': search_text_raw})
                    else: return jsonify({'status': 'not_found', 'message': f'"{cleaned_search_text}"(으)로 시작 상품 없음.'}), 404
                else: return jsonify({'status': 'error', 'message': 'OCR 결과에서 품번 패턴(M...) 못 찾음.'}), 400
            else: return jsonify({'status': 'error', 'message': 'Google Vision API가 텍스트 감지 못함.'}), 400
        except Exception as e: print(f"Server OCR Error (Google Vision): {e}"); return jsonify({'status': 'error', 'message': f'서버 OCR 오류: {e}'}), 500
    return jsonify({'status': 'error', 'message': '파일 처리 중 알 수 없는 오류.'}), 500

@app.route('/text_search', methods=['POST'])
def text_search():
    # ... (이전과 동일) ...
    data = request.json; text = data.get('text', '').strip();
    if not text: return jsonify({'status': 'error', 'message': '텍스트 없음.'}), 400
    search_term = f'%{text}%'; results = Product.query.filter( or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term)) ).all();
    if len(results) == 1: return jsonify({'status': 'found_one', 'product_number': results[0].product_number})
    elif len(results) > 1: return jsonify({'status': 'found_many', 'query': text})
    else: return jsonify({'status': 'not_found', 'message': f'"{text}" 포함 상품 없음.'}), 404

@app.route('/update_stock', methods=['POST'])
def update_stock():
    # ... (이전과 동일) ...
    data = request.json; barcode = data.get('barcode'); change = data.get('change');
    if not barcode or change is None: return jsonify({'status': 'error', 'message': '필수 데이터 누락.'}), 400
    try:
        change = int(change); assert change in [1, -1]; item = Variant.query.filter_by(barcode=barcode).first();
        if item is None: return jsonify({'status': 'error', 'message': '상품(바코드) 없음.'}), 404
        current_stock = item.store_stock; new_stock = max(0, current_stock + change);
        item.store_stock = new_stock; db.session.commit();
        return jsonify({'status': 'success', 'new_quantity': new_stock, 'barcode': barcode})
    except Exception as e: db.session.rollback(); return jsonify({'status': 'error', 'message': f'서버 오류: {e}'}), 500

@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    # ... (이전과 동일) ...
    data = request.json; product_number = data.get('product_number');
    if not product_number: return jsonify({'status': 'error', 'message': '상품 번호 없음.'}), 400
    try:
        product = Product.query.get(product_number);
        if product is None: return jsonify({'status': 'error', 'message': '상품 없음.'}), 404
        product.is_favorite = 1 - product.is_favorite; new_status = product.is_favorite;
        db.session.commit();
        return jsonify({'status': 'success', 'new_favorite_status': new_status})
    except Exception as e: db.session.rollback(); return jsonify({'status': 'error', 'message': f'서버 오류: {e}'}), 500

# --- DB 초기화 명령어 ---
@app.cli.command("init-db")
def init_db_command(): init_db()

# --- Neon DB 깨우기 스케줄러 ---
def keep_db_awake():
    # ... (이전과 동일) ...
    try:
        with app.app_context(): db.session.execute(text('SELECT 1')); print("Neon DB keep-awake query executed.")
    except Exception as e: print(f"Error executing keep-awake query: {e}")
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    # ... (이전과 동일) ...
    scheduler = BackgroundScheduler(daemon=True); scheduler.add_job(keep_db_awake, 'interval', minutes=4); scheduler.start(); print("APScheduler started.")

# --- 앱 실행 ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
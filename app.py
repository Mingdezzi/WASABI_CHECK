import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import io
import os
import re # 정규식 라이브러리

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func, text, Integer, String
from sqlalchemy.orm import joinedload # joinedload 임포트

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
if GCP_CREDENTIALS_PATH and os.path.exists(GCP_CREDENTIALS_PATH):
    try:
        credentials = service_account.Credentials.from_service_account_file(GCP_CREDENTIALS_PATH)
        vision_client = vision.ImageAnnotatorClient(credentials=credentials)
        print("Google Cloud Vision Client initialized successfully.")
    except Exception as e:
        print(f"Error initializing Google Cloud Vision Client: {e}")
else:
    print("GOOGLE_APPLICATION_CREDENTIALS not set or invalid.")
    local_key_path = 'gcp_credentials.json' # 로컬 테스트용 키 파일 이름
    if os.path.exists(local_key_path):
        try:
            credentials = service_account.Credentials.from_service_account_file(local_key_path)
            vision_client = vision.ImageAnnotatorClient(credentials=credentials)
            print("Initialized local Google Vision Client.")
        except Exception as e:
            print(f"Error initializing local Google Vision Client: {e}")

@app.context_processor
def inject_image_url_prefix():
    return dict(IMAGE_URL_PREFIX=IMAGE_URL_PREFIX)

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
    if request.method == 'POST':
        if 'excel_file' not in request.files: flash('파일 선택 안됨.', 'error'); return redirect(url_for('index'))
        file = request.files['excel_file']
        if file.filename == '': flash('파일 선택 안됨.', 'error'); return redirect(url_for('index'))
        if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            try:
                file_content = file.read()
                df = pd.read_excel( io.BytesIO(file_content), sheet_name=0, dtype={'barcode': str, 'product_number': str}, keep_default_na=False )
                required_cols_base = [ 'product_number', 'product_name', 'color', 'barcode', 'size', 'release_year', 'item_category', 'original_price', 'sale_price', 'store_stock', 'hq_stock']
                excel_cols = list(df.columns)
                if not all(col in excel_cols for col in required_cols_base):
                     missing = [col for col in required_cols_base if col not in excel_cols]; flash(f"엑셀 컬럼명 오류. 누락: {missing}", 'error'); return redirect(url_for('index'))
                if 'is_favorite' not in excel_cols: df['is_favorite'] = 0
                else: df['is_favorite'] = pd.to_numeric(df['is_favorite'], errors='coerce').fillna(0).astype(int)

                db.session.query(Variant).delete(); db.session.query(Product).delete(); db.session.commit()

                product_cols = ['product_number', 'product_name', 'release_year', 'item_category', 'is_favorite']
                actual_product_cols = [col for col in product_cols if col in df.columns]
                products_df = df[actual_product_cols].drop_duplicates(subset=['product_number']).copy(); products_df.dropna(subset=['product_number'], inplace=True)
                if 'release_year' in products_df.columns: products_df['release_year'] = pd.to_numeric(products_df['release_year'], errors='coerce').astype('Int64')
                products_data = products_df.to_dict('records'); db.session.bulk_insert_mappings(Product, products_data)

                variant_cols = [ 'barcode', 'product_number', 'color', 'size', 'store_stock', 'hq_stock', 'original_price', 'sale_price' ]
                actual_variant_cols = [col for col in variant_cols if col in df.columns]
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

@app.route('/')
def index():
    query = request.args.get('query', ''); showing_favorites = False
    if query:
        search_term = f'%{query}%'
        products = Product.query.options(joinedload(Product.variants)).filter(
            or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term))
        ).order_by(Product.product_name).all()
    else:
        showing_favorites = True
        products = Product.query.options(joinedload(Product.variants)).filter(
            Product.is_favorite == 1
        ).order_by(Product.item_category, Product.product_name).all()

    return render_template(
        'index.html',
        products=products,
        query=query,
        showing_favorites=showing_favorites,
        showing_all=False,
        is_direct_search_page=False, # 상세 정보 직접 검색 페이지 아님
        advanced_search_params={}
    )

@app.route('/all_products')
def all_products():
    try:
        products = Product.query.options(joinedload(Product.variants)).order_by(Product.item_category, Product.product_name).all()
        return render_template(
            'index.html',
            products=products,
            query="전체 목록",
            showing_favorites=False,
            showing_all=True,
            is_direct_search_page=False, # 상세 정보 직접 검색 페이지 아님
            advanced_search_params={}
        )
    except Exception as e:
        flash(f"전체 목록 조회 오류: {e}", 'error')
        return redirect(url_for('index'))

@app.route('/advanced_search')
def advanced_search():
    try:
        query = Product.query.options(joinedload(Product.variants)).join(Product.variants) # Join 추가

        params = request.args
        search_active = False
        query_summary_parts = []

        # --- Apply filters ---
        if params.get('product_number'):
            value = params.get('product_number')
            query = query.filter(Product.product_number.ilike(f"%{value}%"))
            search_active = True; query_summary_parts.append(f"품번: {value}")
        if params.get('product_name'):
            value = params.get('product_name')
            query = query.filter(Product.product_name.ilike(f"%{value}%"))
            search_active = True; query_summary_parts.append(f"품명: {value}")
        if params.get('color'):
            value = params.get('color')
            query = query.filter(Variant.color.ilike(f"%{value}%"))
            search_active = True; query_summary_parts.append(f"색상: {value}")
        if params.get('size'):
            value = params.get('size')
            query = query.filter(Variant.size.ilike(f"%{value}%"))
            search_active = True; query_summary_parts.append(f"사이즈: {value}")
        if params.get('release_year'):
            try:
                year = int(params.get('release_year'))
                query = query.filter(Product.release_year == year)
                search_active = True; query_summary_parts.append(f"년도: {year}")
            except ValueError: pass
        if params.get('item_category'):
            value = params.get('item_category')
            query = query.filter(Product.item_category.ilike(f"%{value}%"))
            search_active = True; query_summary_parts.append(f"품목: {value}")
        if params.get('original_price_min'):
            try:
                value = int(params.get('original_price_min'))
                query = query.filter(Variant.original_price >= value)
                search_active = True; query_summary_parts.append(f"최초가(min): {value}")
            except ValueError: pass
        if params.get('original_price_max'):
            try:
                value = int(params.get('original_price_max'))
                query = query.filter(Variant.original_price <= value)
                search_active = True; query_summary_parts.append(f"최초가(max): {value}")
            except ValueError: pass
        if params.get('sale_price_min'):
            try:
                value = int(params.get('sale_price_min'))
                query = query.filter(Variant.sale_price >= value)
                search_active = True; query_summary_parts.append(f"판매가(min): {value}")
            except ValueError: pass
        if params.get('sale_price_max'):
            try:
                value = int(params.get('sale_price_max'))
                query = query.filter(Variant.sale_price <= value)
                search_active = True; query_summary_parts.append(f"판매가(max): {value}")
            except ValueError: pass
        if params.get('min_discount'):
            try:
                min_discount_percent = int(params.get('min_discount'))
                if min_discount_percent > 0:
                    ratio = 1.0 - (min_discount_percent / 100.0)
                    query = query.filter(Variant.original_price > 0)
                    query = query.filter(Variant.sale_price <= (Variant.original_price * ratio))
                    search_active = True; query_summary_parts.append(f"할인율: {min_discount_percent}% 이상")
            except ValueError: pass

        if not search_active:
            products = []
            query_summary = "상세 검색: 조건 없음"
        else:
            products = query.distinct().order_by(Product.product_name).all()
            query_summary = f"상세 검색: {', '.join(query_summary_parts)}"

        return render_template(
            'index.html',
            products=products,
            query=query_summary,
            showing_favorites=False,
            showing_all=False,
            is_direct_search_page=False, # 상세 정보 직접 검색 페이지 아님
            advanced_search_params=params
        )
    except Exception as e:
        flash(f"검색 오류: {e}", 'error')
        return redirect(url_for('index'))

@app.route('/direct_search')
def direct_search():
    # 이 페이지는 is_direct_search_page=True로 렌더링
    return render_template('direct_search.html', is_direct_search_page=True)

@app.route('/find_product', methods=['POST'])
def find_product():
    product_number = request.form.get('product_number', '').strip()
    if not product_number:
        flash('품번을 입력해주세요.', 'error')
        return redirect(url_for('direct_search'))

    product = Product.query.get(product_number) # 기본 키로 바로 조회

    if product:
        # 상품 있으면 상세 페이지로 리디렉션
        return redirect(url_for('product_detail', product_number=product.product_number))
    else:
        # 상품 없으면 메시지와 함께 직접 검색 페이지로 다시 리디렉션
        flash(f'품번 "{product_number}"에 해당하는 상품이 없습니다.', 'error')
        return redirect(url_for('direct_search'))

# 정렬 함수
def get_sort_key(variant):
    color = variant.color or ''; size_str = str(variant.size).upper().strip()
    if size_str == '2XS': size_str = 'XXS'
    elif size_str == '2XL': size_str = 'XXL'
    elif size_str == '3XL': size_str = 'XXXL'
    custom_order = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL']
    if size_str.isdigit(): sort_key = (1, int(size_str), '')
    elif size_str in custom_order: sort_key = (2, custom_order.index(size_str), '')
    else: sort_key = (3, 0, size_str)
    return (color, sort_key)

@app.route('/product/<product_number>')
def product_detail(product_number):
    product = Product.query.get(product_number)
    if product is None: flash("상품 없음.", 'error'); return redirect(url_for('index'))
    image_product_number = product.product_number.split(' ')[0]
    image_url = f"{IMAGE_URL_PREFIX}{image_product_number}.jpg"; variants_list = sorted(product.variants, key=get_sort_key); related_products = []
    if product.product_name:
        search_words = product.product_name.split(' ');
        if search_words:
            search_term = search_words[-1]
            if len(search_term) > 1:
                related_products = Product.query.filter( Product.product_name.ilike(f'%{search_term}%'), Product.product_number != product_number ).limit(5).all()

    return render_template(
        'detail.html',
        product=product,
        image_url=image_url,
        variants=variants_list,
        related_products=related_products,
        showing_all=False,
        is_direct_search_page=False, # 상세 페이지는 직접 검색 페이지 아님
        advanced_search_params={}
    )

# --- API 라우트 ---
@app.route('/barcode_search', methods=['POST'])
def barcode_search():
    data = request.json; barcode = data.get('barcode')
    if not barcode: return jsonify({'status': 'error', 'message': '바코드 없음.'}), 400
    scanned_clean = barcode.replace('-', '').strip()
    if len(scanned_clean) < 11: return jsonify({'status': 'error', 'message': f'바코드 짧음 ({len(scanned_clean)}자리).'}), 400
    variant = Variant.query.filter( func.replace(Variant.barcode, '-', '').startswith(scanned_clean) ).first()
    if variant: return jsonify({'status': 'success', 'product_number': variant.product_number})
    else: return jsonify({'status': 'error', 'message': 'DB에 일치하는 바코드 없음.'}), 404

@app.route('/ocr_upload', methods=['POST'])
def ocr_upload():
    if vision_client is None: return jsonify({'status': 'error', 'message': 'Google Cloud Vision 클라이언트 초기화 실패.'}), 500
    if 'ocr_image' not in request.files: return jsonify({'status': 'error', 'message': '이미지 파일 없음.'}), 400
    file = request.files['ocr_image']
    if file.filename == '': return jsonify({'status': 'error', 'message': '파일 이름 없음.'}), 400
    if file:
        try:
            content = file.read(); image = vision.Image(content=content)
            response = vision_client.text_detection(image=image); texts = response.text_annotations
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
    data = request.json
    text_raw = data.get('text', '').strip()
    if not text_raw:
        return jsonify({'status': 'error', 'message': '검색할 텍스트가 없습니다.'}), 400
    search_term_asis = f'%{text_raw}%'
    text_cleaned = text_raw.replace(' ', '')
    search_term_cleaned = f'%{text_cleaned}%'
    results = Product.query.filter(
        or_(
            Product.product_name.ilike(search_term_asis),
            Product.product_number.ilike(search_term_asis),
            Product.product_number.ilike(search_term_cleaned)
        )
    ).all()
    if len(results) == 1:
        return jsonify({'status': 'found_one', 'product_number': results[0].product_number})
    elif len(results) > 1:
        return jsonify({'status': 'found_many', 'query': text_raw})
    else:
        return jsonify({'status': 'not_found', 'message': f'"{text_raw}" 포함 상품 없음.'}), 404

@app.route('/update_stock', methods=['POST'])
def update_stock():
    data = request.json; barcode = data.get('barcode'); change = data.get('change')
    if not barcode or change is None: return jsonify({'status': 'error', 'message': '필수 데이터 누락.'}), 400
    try:
        change = int(change); assert change in [1, -1]
        item = Variant.query.filter_by(barcode=barcode).first()
        if item is None: return jsonify({'status': 'error', 'message': '상품(바코드) 없음.'}), 404
        current_stock = item.store_stock; new_stock = max(0, current_stock + change)
        item.store_stock = new_stock; db.session.commit()
        return jsonify({'status': 'success', 'new_quantity': new_stock, 'barcode': barcode})
    except Exception as e: db.session.rollback(); return jsonify({'status': 'error', 'message': f'서버 오류: {e}'}), 500

@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    data = request.json; product_number = data.get('product_number')
    if not product_number: return jsonify({'status': 'error', 'message': '상품 번호 없음.'}), 400
    try:
        product = Product.query.get(product_number)
        if product is None: return jsonify({'status': 'error', 'message': '상품 없음.'}), 404
        product.is_favorite = 1 - product.is_favorite; new_status = product.is_favorite
        db.session.commit()
        return jsonify({'status': 'success', 'new_favorite_status': new_status})
    except Exception as e: db.session.rollback(); return jsonify({'status': 'error', 'message': f'서버 오류: {e}'}), 500

# --- DB 초기화 명령어 ---
@app.cli.command("init-db")
def init_db_command(): init_db()

# --- Neon DB 깨우기 스케줄러 ---
def keep_db_awake():
    try:
        with app.app_context(): db.session.execute(text('SELECT 1')); print("Neon DB keep-awake query executed.")
    except Exception as e: print(f"Error executing keep-awake query: {e}")
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    scheduler = BackgroundScheduler(daemon=True); scheduler.add_job(keep_db_awake, 'interval', minutes=4); scheduler.start(); print("APScheduler started.")

# --- 앱 실행 ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

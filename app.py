import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import io
import os
import re # 정규식 라이브러리 (한 번만 import)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func, text

from apscheduler.schedulers.background import BackgroundScheduler

# 서버 OCR 라이브러리
import pytesseract
from PIL import Image
from werkzeug.utils import secure_filename # (한 번만 import)

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

# --- DB 모델 정의 ---
class Product(db.Model):
    __tablename__ = 'products'
    product_number = db.Column(db.String, primary_key=True)
    product_name = db.Column(db.String, nullable=False)
    is_favorite = db.Column(db.Integer, default=0)
    variants = db.relationship('Variant', backref='product', lazy=True, cascade="all, delete-orphan")

class Variant(db.Model):
    __tablename__ = 'variants'
    barcode = db.Column(db.String, primary_key=True)
    product_number = db.Column(db.String, db.ForeignKey('products.product_number'), nullable=False)
    color = db.Column(db.String)
    size = db.Column(db.String)
    store_stock = db.Column(db.Integer, default=0)
    hq_stock = db.Column(db.Integer, default=0)
    original_price = db.Column(db.Integer, default=0)
    sale_price = db.Column(db.Integer, default=0)
    discount_rate = db.Column(db.String)

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
                required_cols = [ 'product_number', 'product_name', 'color', 'barcode', 'size', 'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate' ]
                if not all(col in df.columns for col in required_cols): flash(f"엑셀 컬럼명 오류. 필수 10개 확인: {required_cols}", 'error'); return redirect(url_for('index'))
                if 'is_favorite' not in df.columns: df['is_favorite'] = 0
                db.session.query(Product).delete(); db.session.commit()
                products_df = df[['product_number', 'product_name', 'is_favorite']].drop_duplicates(subset=['product_number']).copy(); products_df.dropna(subset=['product_number'], inplace=True)
                products_df['is_favorite'] = pd.to_numeric(products_df['is_favorite'], errors='coerce').fillna(0).astype(int)
                products_data = products_df.to_dict('records'); db.session.bulk_insert_mappings(Product, products_data)
                variants_cols = [ 'barcode', 'product_number', 'color', 'size', 'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate' ]
                variants_df = df[variants_cols].copy(); variants_df.dropna(subset=['barcode'], inplace=True)
                variants_data = variants_df.to_dict('records'); db.session.bulk_insert_mappings(Variant, variants_data)
                db.session.commit()
                flash(f"성공 ({file.filename}): {len(products_df)}개 상품, {len(variants_df)}개 SKU 임포트.", 'success')
            except Exception as e: db.session.rollback(); flash(f"임포트 오류: {e}", 'error')
            return redirect(url_for('index'))
        else: flash('엑셀 파일만 업로드 가능.', 'error'); return redirect(url_for('index'))
    return redirect(url_for('index'))

# --- 웹페이지 라우트 ---
@app.route('/')
def index():
    query = request.args.get('query', ''); showing_favorites = False
    if query: search_term = f'%{query}%'; products = Product.query.filter( or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term)) ).order_by(Product.product_name).all()
    else: showing_favorites = True; products = Product.query.filter_by(is_favorite=1).order_by(Product.product_name).all()
    return render_template('index.html', products=products, query=query, showing_favorites=showing_favorites)

# 정렬 함수
def get_sort_key(variant):
    color = variant.color or ''
    size_str = str(variant.size).upper().strip()

    # 사이즈 동의어 처리
    if size_str == '2XS':
        size_str = 'XXS'
    elif size_str == '2XL':
        size_str = 'XXL'
    elif size_str == '3XL':
        size_str = 'XXXL'

    custom_order = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL']

    # 정렬 키 생성
    if size_str.isdigit():
        sort_key = (1, int(size_str), '') # 숫자 우선
    elif size_str in custom_order:
        sort_key = (2, custom_order.index(size_str), '') # 커스텀 알파벳 순서
    else:
        sort_key = (3, 0, size_str) # 나머지 알파벳

    return (color, sort_key) # 최종 키: (컬러, (사이즈 종류, 사이즈 값, 원본 문자열))

@app.route('/product/<product_number>')
def product_detail(product_number):
    product = Product.query.get(product_number)
    if product is None: flash("상품 없음.", 'error'); return redirect(url_for('index'))
    image_url = f"{IMAGE_URL_PREFIX}{product.product_number}.jpg"; variants_list = sorted(product.variants, key=get_sort_key); related_products = []
    # 연관 상품 로직
    if product.product_name:
        search_words = product.product_name.split(' ')
        if search_words:
            search_term = search_words[-1]
            if len(search_term) > 1:
                related_products = Product.query.filter(
                    Product.product_name.ilike(f'%{search_term}%'),
                    Product.product_number != product_number
                ).limit(5).all()
    return render_template( 'detail.html', product=product, image_url=image_url, variants=variants_list, related_products=related_products )


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

# 서버 OCR API
@app.route('/ocr_upload', methods=['POST'])
def ocr_upload():
    if 'ocr_image' not in request.files: return jsonify({'status': 'error', 'message': '이미지 파일 없음.'}), 400
    file = request.files['ocr_image']
    if file.filename == '': return jsonify({'status': 'error', 'message': '파일 이름 없음.'}), 400
    if file:
        try:
            img = Image.open(file.stream)
            custom_config = r'--oem 3 --psm 6 -l kor+eng'
            ocr_text = pytesseract.image_to_string(img, config=custom_config)
            print(f"Server OCR Raw Text: {ocr_text}") # 디버깅용

            cleaned_text = ocr_text.upper().replace('\n', ' ').replace('\r', ' ')
            cleaned_text = re.sub(r'\s+', ' ', cleaned_text) # 여러 공백 -> 하나로

            product_number_pattern = r'\bM[A-Z0-9-]{4,}\b'
            matches = re.findall(product_number_pattern, cleaned_text)
            print(f"Found Product Number Candidates: {matches}") # 디버깅용

            if matches:
                search_text = matches[0] # 첫 번째 후보 사용
                search_term = f'%{search_text}%'
                results = Product.query.filter(
                    or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term))
                ).all()

                if len(results) == 1:
                    return jsonify({'status': 'found_one', 'product_number': results[0].product_number})
                elif len(results) > 1:
                    return jsonify({'status': 'found_many', 'query': search_text})
                else:
                    return jsonify({'status': 'not_found', 'message': f'"{search_text}" 상품 없음.'}), 404
            else:
                return jsonify({'status': 'error', 'message': 'OCR 결과에서 품번 패턴(M...) 못 찾음.'}), 400
        except Exception as e:
            print(f"Server OCR Error: {e}") # 디버깅용
            return jsonify({'status': 'error', 'message': f'서버 OCR 오류: {e}'}), 500

    return jsonify({'status': 'error', 'message': '파일 처리 중 알 수 없는 오류.'}), 500


@app.route('/text_search', methods=['POST'])
def text_search():
    data = request.json; text = data.get('text', '').strip()
    if not text: return jsonify({'status': 'error', 'message': '텍스트 없음.'}), 400
    search_term = f'%{text}%'
    results = Product.query.filter( or_(Product.product_number.ilike(search_term), Product.product_name.ilike(search_term)) ).all()
    if len(results) == 1: return jsonify({'status': 'found_one', 'product_number': results[0].product_number})
    elif len(results) > 1: return jsonify({'status': 'found_many', 'query': text})
    else: return jsonify({'status': 'not_found', 'message': f'"{text}" 포함 상품 없음.'}), 404

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
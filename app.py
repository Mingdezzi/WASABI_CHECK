import sqlite3
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, g, flash, jsonify
import io 
import os 

from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# --- DB 설정 ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 
    'sqlite:///' + os.path.join(app.root_path, 'database.db')
)
app.config['SECRET_KEY'] = 'wasabi-check-secret-key'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False 

db = SQLAlchemy(app) 

IMAGE_URL_PREFIX = 'https://files.ebizway.co.kr/files/10249/Style/'

# --- (2. DB 모델(테이블) 정의) ---
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

# --- (3. init_db 함수 수정) ---
def init_db():
    """
    SQLAlchemy를 사용하여 DB 테이블을 생성합니다.
    """
    with app.app_context():
        db.create_all() 
        print("WASABI_CHECK: SQLAlchemy DB 테이블이 성공적으로 초기화/검증되었습니다.")

# --- (4. import_excel 함수 수정) ---
@app.route('/import_excel', methods=['GET', 'POST'])
def import_excel():
    """
    (A6) 엑셀 데이터를 DB로 덮어씁니다. (SQLAlchemy 버전)
    """
    if request.method == 'POST':
        if 'excel_file' not in request.files:
            flash('오류: 파일이 선택되지 않았습니다.', 'error')
            return redirect(url_for('index'))

        file = request.files['excel_file']
        if file.filename == '':
            flash('오류: 파일이 선택되지 않았습니다.', 'error')
            return redirect(url_for('index'))

        if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            try:
                file_content = file.read()
                df = pd.read_excel(
                    io.BytesIO(file_content),
                    sheet_name=0,
                    dtype={'barcode': str, 'product_number': str},
                    keep_default_na=False 
                )

                required_cols = [
                    'product_number', 'product_name', 'color', 'barcode', 'size',
                    'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate'
                ]
                if not all(col in df.columns for col in required_cols):
                    flash(f"엑셀 컬럼명 오류. 필수 10개 컬럼이 모두 있는지 확인하세요: {required_cols}", 'error')
                    return redirect(url_for('index'))

                if 'is_favorite' not in df.columns:
                    df['is_favorite'] = 0
                
                db.session.query(Product).delete()
                db.session.commit()

                products_df = df[['product_number', 'product_name', 'is_favorite']].drop_duplicates(subset=['product_number']).copy()
                products_df.dropna(subset=['product_number'], inplace=True)
                products_df['is_favorite'] = pd.to_numeric(products_df['is_favorite'], errors='coerce').fillna(0).astype(int)
                
                products_data = products_df.to_dict('records')
                db.session.bulk_insert_mappings(Product, products_data)

                variants_cols = [
                    'barcode', 'product_number', 'color', 'size',
                    'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate'
                ]
                variants_df = df[variants_cols].copy()
                variants_df.dropna(subset=['barcode'], inplace=True)
                
                variants_data = variants_df.to_dict('records')
                db.session.bulk_insert_mappings(Variant, variants_data)
                
                db.session.commit()
                
                flash(f"성공 ({file.filename}): {len(products_df)}개 상품, {len(variants_df)}개 SKU를 임포트했습니다.", 'success')

            except Exception as e:
                db.session.rollback() 
                flash(f"임포트 중 심각한 오류 발생: {e}", 'error')
            
            return redirect(url_for('index'))
        else:
            flash('오류: .xlsx 또는 .xls 엑셀 파일만 업로드할 수 있습니다.', 'error')
            return redirect(url_for('index'))
    return redirect(url_for('index'))


# --- (5. 모든 라우트(API) 수정) ---

@app.route('/')
def index():
    query = request.args.get('query', '') 
    showing_favorites = False

    if query:
        search_term = f'%{query}%'
        
        # (A3) 대소문자 구분 없이 검색 (.ilike)
        products = Product.query.filter(
            (Product.product_number.ilike(search_term)) | 
            (Product.product_name.ilike(search_term))
        ).order_by(Product.product_name).all()
        
    else:
        showing_favorites = True
        products = Product.query.filter_by(is_favorite=1).order_by(Product.product_name).all()
        
    return render_template('index.html', products=products, query=query, showing_favorites=showing_favorites)


# (*** 1. 수정: 사이즈 정렬을 위한 헬퍼 함수 ***)
def get_sort_key(variant):
    """
    (Req 1) 사이즈 정렬을 위한 커스텀 정렬 키 함수
    1순위: 컬러 (ABC 순)
    2순위: 사이즈 (숫자 -> 커스텀 알파벳 -> 기타 알파벳 순)
    """
    color = variant.color or ''
    size_str = str(variant.size).upper().strip()
    
    # (b) 알파벳 사이즈 동의어 처리 (2XS -> XXS)
    if size_str == '2XS': size_str = 'XXS'
    if size_str == '2XL': size_str = 'XXL'
    if size_str == '3XL': size_str = 'XXXL'
    
    # (b) 커스텀 알파벳 순서 정의
    custom_order = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL']
    
    # (a) 사이즈가 숫자인 경우
    if size_str.isdigit():
        sort_key = (1, int(size_str), '')
    # (b) 사이즈가 커스텀 알파벳인 경우
    elif size_str in custom_order:
        sort_key = (2, custom_order.index(size_str), '')
    # (c) 그 외 (일반 알파벳)
    else:
        sort_key = (3, 0, size_str)
        
    return (color, sort_key)


@app.route('/product/<product_number>')
def product_detail(product_number):
    product = Product.query.get(product_number)
    
    if product is None:
        flash("상품을 찾을 수 없습니다.", 'error')
        return redirect(url_for('index'))
        
    image_url = f"{IMAGE_URL_PREFIX}{product.product_number}.jpg"
    
    # (*** 1. 수정: 커스텀 정렬 적용 ***)
    # 기존: sorted(product.variants, key=lambda v: (v.color or '', v.size or ''))
    variants_list = sorted(product.variants, key=get_sort_key)
    
    # (*** 2. 수정: 연관 상품 로직 추가 ***)
    related_products = []
    if product.product_name:
        search_words = product.product_name.split(' ')
        if len(search_words) > 0:
            # 품명에서 마지막 단어(예: '티셔츠')를 검색어로 사용
            search_term = search_words[-1] 
            if len(search_term) > 1: # 1글자짜리 단어는 무시
                related_products = Product.query.filter(
                    Product.product_name.ilike(f'%{search_term}%'),  # 비슷한 품명
                    Product.product_number != product_number       # 현재 상품 제외
                ).limit(5).all() # 최대 5개

    return render_template(
        'detail.html', 
        product=product, 
        image_url=image_url, 
        variants=variants_list,
        related_products=related_products # (2) 연관 상품 리스트 전달
    )

@app.route('/barcode_search', methods=['POST'])
def barcode_search():
    """ (A1) 바코드 스캔 API (15자리 부분 일치) """
    data = request.json
    barcode = data.get('barcode') # 예: 18자리 스캔
    
    if not barcode:
        return jsonify({'status': 'error', 'message': '바코드가 전송되지 않았습니다.'}), 400
        
    search_barcode = barcode[:15] 
    
    variant = Variant.query.filter_by(barcode=search_barcode).first()
    
    if variant:
        return jsonify({'status': 'success', 'product_number': variant.product_number})
    else:
        return jsonify({'status': 'error', 'message': f'해당 바코드({search_barcode})를 찾을 수 없습니다.'}), 404

@app.route('/update_stock', methods=['POST'])
def update_stock():
    data = request.json
    barcode = data.get('barcode')
    change = data.get('change')

    if not barcode or change is None:
        return jsonify({'status': 'error', 'message': '필수 데이터가 누락되었습니다.'}), 400

    try:
        change = int(change) 
        if change not in [1, -1]:
             raise ValueError("Change 값은 1 또는 -1 이어야 합니다.")

        item = Variant.query.filter_by(barcode=barcode).first()

        if item is None:
            return jsonify({'status': 'error', 'message': '해당 상품(바코드)을 찾을 수 없습니다.'}), 404

        current_stock = item.store_stock
        new_stock = current_stock + change
        if new_stock < 0:
            new_stock = 0

        item.store_stock = new_stock
        db.session.commit()

        return jsonify({'status': 'success', 'new_quantity': new_stock, 'barcode': barcode})

    except Exception as e:
        db.session.rollback() 
        return jsonify({'status': 'error', 'message': f'서버 오류 발생: {e}'}), 500

@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    data = request.json
    product_number = data.get('product_number')

    if not product_number:
        return jsonify({'status': 'error', 'message': '상품 번호가 없습니다.'}), 400

    try:
        product = Product.query.get(product_number)

        if product is None:
            return jsonify({'status': 'error', 'message': '상품을 찾을 수 없습니다.'}), 404

        product.is_favorite = 1 - product.is_favorite
        new_status = product.is_favorite
        
        db.session.commit()

        return jsonify({'status': 'success', 'new_favorite_status': new_status})

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': f'서버 오류 발생: {e}'}), 500


# --- (DB 초기화 명령어) ---
@app.cli.command("init-db")
def init_db_command():
    """CLI에서 'flask init-db'를 실행하면 DB 테이블을 생성합니다."""
    init_db()


# --- 앱 실행 ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
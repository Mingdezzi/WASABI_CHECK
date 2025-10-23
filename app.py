import sqlite3
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, g, flash, jsonify
import io 
import os # (추가) 환경 변수(DATABASE_URL)를 읽기 위해

# --- (1. SQLAlchemy 설정으로 변경) ---
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# --- DB 설정 ---
# (중요!) 
# 1. 서버(Render)에 'DATABASE_URL'이 설정되어 있으면 -> PostgreSQL 사용
# 2. 아니면 (내 컴퓨터) -> sqlite:///database.db (로컬 파일) 사용
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 
    'sqlite:///' + os.path.join(app.root_path, 'database.db')
)
app.config['SECRET_KEY'] = 'wasabi-check-secret-key'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # 권장 설정

db = SQLAlchemy(app) # DB 객체 생성

IMAGE_URL_PREFIX = 'https://files.ebizway.co.kr/files/10249/Style/'

# --- (2. DB 모델(테이블) 정의) ---
# products 테이블을 파이썬 클래스로 정의
class Product(db.Model):
    __tablename__ = 'products'
    product_number = db.Column(db.String, primary_key=True)
    product_name = db.Column(db.String, nullable=False)
    is_favorite = db.Column(db.Integer, default=0)
    # (관계 정의) 이 Product가 여러 Variant를 가짐
    variants = db.relationship('Variant', backref='product', lazy=True, cascade="all, delete-orphan")

# variants 테이블을 파이썬 클래스로 정의
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
        # (기존 DB 마이그레이션 로직 삭제)
        # 정의된 모델(Product, Variant)을 기반으로 테이블 생성
        db.create_all() 
        print("WASABI_CHECK: SQLAlchemy DB 테이블이 성공적으로 초기화/검증되었습니다.")

# --- 헬퍼 함수 삭제 ---
# (get_db, close_connection 함수는 SQLAlchemy가 알아서 하므로 삭제)

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
                # (Fix 1) "NA" 컬러를 문자열로 인식
                df = pd.read_excel(
                    io.BytesIO(file_content),
                    sheet_name=0,
                    dtype={'barcode': str, 'product_number': str},
                    keep_default_na=False 
                )

                # 엑셀 파일 검증
                required_cols = [
                    'product_number', 'product_name', 'color', 'barcode', 'size',
                    'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate'
                ]
                if not all(col in df.columns for col in required_cols):
                    flash(f"엑셀 컬럼명 오류. 필수 10개 컬럼이 모두 있는지 확인하세요: {required_cols}", 'error')
                    return redirect(url_for('index'))

                # (Fix 2) 엑셀에 'is_favorite'가 없으면 0으로 채움
                if 'is_favorite' not in df.columns:
                    df['is_favorite'] = 0
                
                # 기존 데이터 덮어쓰기 (SQLAlchemy 방식)
                # (주의: cascade="all, delete-orphan" 설정으로 Product만 지워도 Variant가 자동 삭제됨)
                db.session.query(Product).delete()
                db.session.commit() # 일단 삭제 확정

                # 3. 데이터 삽입 (SQLAlchemy 방식)
                
                # 3-1. products 데이터 준비 (중복 제거)
                products_df = df[['product_number', 'product_name', 'is_favorite']].drop_duplicates(subset=['product_number']).copy()
                products_df.dropna(subset=['product_number'], inplace=True)
                products_df['is_favorite'] = pd.to_numeric(products_df['is_favorite'], errors='coerce').fillna(0).astype(int)
                
                # DataFrame -> Dictionary 리스트로 변환
                products_data = products_df.to_dict('records')
                # (속도 향상) 대량 삽입 (bulk_insert_mappings)
                db.session.bulk_insert_mappings(Product, products_data)

                # 3-2. variants 데이터 준비
                variants_cols = [
                    'barcode', 'product_number', 'color', 'size',
                    'store_stock', 'hq_stock', 'original_price', 'sale_price', 'discount_rate'
                ]
                variants_df = df[variants_cols].copy()
                variants_df.dropna(subset=['barcode'], inplace=True)
                
                variants_data = variants_df.to_dict('records')
                db.session.bulk_insert_mappings(Variant, variants_data)
                
                # 4. 변경사항 저장
                db.session.commit()
                
                flash(f"성공 ({file.filename}): {len(products_df)}개 상품, {len(variants_df)}개 SKU를 임포트했습니다.", 'success')

            except Exception as e:
                db.session.rollback() # 오류 발생 시 롤백 (SQLAlchemy 방식)
                flash(f"임포트 중 심각한 오류 발생: {e}", 'error')
            
            return redirect(url_for('index'))
        else:
            flash('오류: .xlsx 또는 .xls 엑셀 파일만 업로드할 수 있습니다.', 'error')
            return redirect(url_for('index'))
    return redirect(url_for('index'))


# --- (5. 모든 라우트(API) 수정) ---

@app.route('/')
def index():
    """
    (메인) 즐겨찾기 또는 검색 (SQLAlchemy 쿼리)
    """
    query = request.args.get('query', '') 
    showing_favorites = False

    if query:
        # (A3) 검색어가 있으면? -> (SQLAlchemy 쿼리)
        search_term = f'%{query}%'
        # Product.query.filter(...)
        products = Product.query.filter(
            (Product.product_number.like(search_term)) | 
            (Product.product_name.like(search_term))
        ).order_by(Product.product_name).all()
    else:
        # (A1) 검색어가 없으면? -> (SQLAlchemy 쿼리)
        showing_favorites = True
        products = Product.query.filter_by(is_favorite=1).order_by(Product.product_name).all()
        
    return render_template('index.html', products=products, query=query, showing_favorites=showing_favorites)


@app.route('/product/<product_number>')
def product_detail(product_number):
    """
    상품 상세 페이지 (SQLAlchemy 쿼리)
    """
    # 1. 상품 기본 정보 (primary key로 검색: .get())
    product = Product.query.get(product_number)
    
    if product is None:
        flash("상품을 찾을 수 없습니다.", 'error')
        return redirect(url_for('index'))
        
    # 2. 이미지 URL 동적 생성
    image_url = f"{IMAGE_URL_PREFIX}{product.product_number}.jpg"
    
    # 3. 상세 재고/가격 정보 (.variants 관계 사용)
    # (SQLAlchemy가 알아서 variants 테이블을 조회해 줌)
    # (정렬만 추가)
    variants_list = sorted(product.variants, key=lambda v: (v.color or '', v.size or ''))
    
    return render_template('detail.html', product=product, image_url=image_url, variants=variants_list)

@app.route('/barcode_search', methods=['POST'])
def barcode_search():
    """ (A1) 바코드 스캔 API (SQLAlchemy 쿼리) """
    data = request.json
    barcode = data.get('barcode')
    
    if not barcode:
        return jsonify({'status': 'error', 'message': '바코드가 전송되지 않았습니다.'}), 400
        
    # .filter_by로 검색 후 .first() (첫 번째 1개)
    variant = Variant.query.filter_by(barcode=barcode).first()
    
    if variant:
        return jsonify({'status': 'success', 'product_number': variant.product_number})
    else:
        return jsonify({'status': 'error', 'message': '해당 바코드를 찾을 수 없습니다.'}), 404

@app.route('/update_stock', methods=['POST'])
def update_stock():
    """ (A5) 재고 증가/감소 API (SQLAlchemy 쿼리) """
    data = request.json
    barcode = data.get('barcode')
    change = data.get('change')

    if not barcode or change is None:
        return jsonify({'status': 'error', 'message': '필수 데이터가 누락되었습니다.'}), 400

    try:
        change = int(change) 
        if change not in [1, -1]:
             raise ValueError("Change 값은 1 또는 -1 이어야 합니다.")

        # 1. DB에서 아이템 찾기
        item = Variant.query.filter_by(barcode=barcode).first()

        if item is None:
            return jsonify({'status': 'error', 'message': '해당 상품(바코드)을 찾을 수 없습니다.'}), 404

        current_stock = item.store_stock
        new_stock = current_stock + change
        if new_stock < 0:
            new_stock = 0

        # 2. (중요) 파이썬 객체의 값을 변경
        item.store_stock = new_stock
        
        # 3. DB에 커밋 (저장)
        db.session.commit()

        return jsonify({'status': 'success', 'new_quantity': new_stock, 'barcode': barcode})

    except Exception as e:
        db.session.rollback() # 롤백
        return jsonify({'status': 'error', 'message': f'서버 오류 발생: {e}'}), 500

@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    """ (A3) 즐겨찾기 추가/해제 API (SQLAlchemy 쿼리) """
    data = request.json
    product_number = data.get('product_number')

    if not product_number:
        return jsonify({'status': 'error', 'message': '상품 번호가 없습니다.'}), 400

    try:
        # 1. DB에서 상품 찾기 (.get()은 primary key로 검색)
        product = Product.query.get(product_number)

        if product is None:
            return jsonify({'status': 'error', 'message': '상품을 찾을 수 없습니다.'}), 404

        # 2. 상태 뒤집기 (파이썬 객체 값 변경)
        product.is_favorite = 1 - product.is_favorite
        new_status = product.is_favorite
        
        # 3. DB에 커밋 (저장)
        db.session.commit()

        return jsonify({'status': 'success', 'new_favorite_status': new_status})

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': f'서버 오류 발생: {e}'}), 500


# --- 앱 실행 ---
if __name__ == '__main__':
    # (init_db_with_migration 대신 init_db() 호출)
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
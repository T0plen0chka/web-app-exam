import os
import hashlib
import bleach
import markdown
from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from models import db, User, Role, Book, Genre, Cover, Review

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Включаем поддержку внешних ключей и CASCADE для SQLite
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy import func
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    # Регистрируем функцию нижнего регистра Python внутри SQLite для корректного ILIKE с кириллицей
    dbapi_connection.create_function("lower", 1, lambda s: s.lower() if s else "")
    
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Для выполнения данного действия необходимо пройти процедуру аутентификации"
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Функция проверки прав доступа по декораторам
def check_rights(action, record=None):
    if not current_user.is_authenticated:
        return False
    if current_user.role.name == 'администратор':
        return True
    if current_user.role.name == 'модератор' and action in ['edit', 'moderate']:
        return True
    if current_user.role.name == 'пользователь' and action == 'review':
        return True
    return False

# Кастомный фильтр для рендеринга Markdown во вьюхах
@app.template_filter('markdown')
def convert_markdown(text):
    clean_text = bleach.clean(text, tags=['p', 'strong', 'em', 'u', 'h1', 'h2', 'h3', 'ul', 'ol', 'li', 'br'])
    return markdown.markdown(clean_text)

@app.route('/')
def index():
    # Данные для фильтра "Год" динамически из БД
    years = [r[0] for r in db.session.query(Book.year).distinct().order_by(Book.year.desc()).all()]
    genres_list = Genre.query.all()

    # Сбор параметров поиска (Вариант 3)
    title = request.args.get('title', '')
    author = request.args.get('author', '')
    selected_genres = request.args.getlist('genres')
    selected_years = request.args.getlist('years')
    pages_from = request.args.get('pages_from', '')
    pages_to = request.args.get('pages_to', '')
    page = request.args.get('page', 1, type=int)

    query = Book.query

    if title:
        query = query.filter(Book.title.ilike(f'%{title}%'))
    if author:
        query = query.filter(Book.author.ilike(f'%{author}%'))
    if selected_genres:
        query = query.filter(Book.genres.any(Genre.id.in_(selected_genres)))
    if selected_years:
        query = query.filter(Book.year.in_(selected_years))
    if pages_from:
        query = query.filter(Book.pages >= int(pages_from))
    if pages_to:
        query = query.filter(Book.pages <= int(pages_to))

    # Сортировка: сначала новые
    query = query.order_by(Book.created_at.desc())
    
    pagination = query.paginate(page=page, per_page=10, error_out=False)
    books = pagination.items

    # Сбор статистики для вывода в шаблон
    books_data = []
    for book in books:
        avg_rating = db.session.query(db.func.avg(Review.rating)).filter(Review.book_id == book.id).scalar() or 0
        review_count = Review.query.filter_by(book_id=book.id).count()
        books_data.append({
            'obj': book,
            'avg_rating': round(avg_rating, 1),
            'review_count': review_count
        })

    return render_template('index.html', books_data=books_data, pagination=pagination, 
                           years=years, genres_list=genres_list, check_rights=check_rights)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_val = request.form.get('login')
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False

        user = User.query.filter_by(login=login_val).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            return redirect(url_for('index'))
        
        flash('Невозможно аутентифицироваться с указанными логином и паролем', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/book/add', methods=['GET', 'POST'])
@login_required
def add_book():
    if not check_rights('add'):
        flash('У вас недостаточно прав для выполнения данного действия', 'danger')
        return redirect(url_for('index'))
        
    genres = Genre.query.all()
    if request.method == 'POST':
        try:
            title = bleach.clean(request.form.get('title'))
            description = bleach.clean(request.form.get('description'))
            year = int(request.form.get('year'))
            publisher = bleach.clean(request.form.get('publisher'))
            author = bleach.clean(request.form.get('author'))
            pages = int(request.form.get('pages'))
            genre_ids = request.form.getlist('genres')

            new_book = Book(title=title, description=description, year=year, publisher=publisher, author=author, pages=pages)
            for g_id in genre_ids:
                g = Genre.query.get(g_id)
                if g: new_book.genres.append(g)

            db.session.add(new_book)
            db.session.flush() # Получаем ID новой книги до коммита

            # Работа с обложкой
            file = request.files.get('cover')
            if file and file.filename != '':
                file_content = file.read()
                md5_hash = hashlib.md5(file_content).hexdigest()
                file.seek(0) # Сброс указателя файла после чтения хэша

                # Проверяем, есть ли уже такой файл по хэшу
                existing_cover = Cover.query.filter_by(md5_hash=md5_hash).first()
                
                if existing_cover:
                    new_cover = Cover(filename=existing_cover.filename, mime_type=file.content_type, md5_hash=md5_hash, book_id=new_book.id)
                else:
                    filename = secure_filename(f"{new_book.id}_{file.filename}")
                    new_cover = Cover(filename=filename, mime_type=file.content_type, md5_hash=md5_hash, book_id=new_book.id)
                    # Сохраняем физически на диск
                    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

                db.session.add(new_cover)

            db.session.commit()
            return redirect(url_for('view_book', book_id=new_book.id))
        except Exception as e:
            db.session.rollback()
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')
            
    return render_template('book_form.html', action='add', genres=genres, book=None)

@app.route('/book/<int:book_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_book(book_id):
    if not check_rights('edit'):
        flash('У вас недостаточно прав для выполнения данного действия', 'danger')
        return redirect(url_for('index'))

    book = Book.query.get_or_404(book_id)
    genres = Genre.query.all()

    if request.method == 'POST':
        try:
            book.title = bleach.clean(request.form.get('title'))
            book.description = bleach.clean(request.form.get('description'))
            book.year = int(request.form.get('year'))
            book.publisher = bleach.clean(request.form.get('publisher'))
            book.author = bleach.clean(request.form.get('author'))
            book.pages = int(request.form.get('pages'))

            genre_ids = request.form.getlist('genres')
            book.genres = []
            for g_id in genre_ids:
                g = Genre.query.get(g_id)
                if g: book.genres.append(g)

            db.session.commit()
            return redirect(url_for('view_book', book_id=book.id))
        except Exception:
            db.session.rollback()
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')

    return render_template('book_form.html', action='edit', genres=genres, book=book)

@app.route('/book/<int:book_id>/delete', methods=['POST'])
@login_required
def delete_book(book_id):
    if not current_user.is_authenticated or current_user.role.name != 'администратор':
        flash('У вас недостаточно прав для выполнения данного действия', 'danger')
        return redirect(url_for('index'))

    book = Book.query.get_or_404(book_id)
    try:
        # Если у обложки уникальный файл, удаляем его из ОС
        if book.cover:
            same_file_covers = Cover.query.filter_by(filename=book.cover.filename).count()
            if same_file_covers == 1: # Файл используется только этой книгой
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], book.cover.filename)
                if os.path.exists(file_path):
                    os.remove(file_path)

        db.session.delete(book)
        db.session.commit()
        flash('Книга успешно удалена!', 'success')
    except Exception:
        db.session.rollback()
        flash('Ошибка при удалении книги.', 'danger')

    return redirect(url_for('index'))

@app.route('/book/<int:book_id>')
def view_book(book_id):
    book = Book.query.get_or_404(book_id)
    user_review = None
    if current_user.is_authenticated:
        user_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
    return render_template('book_view.html', book=book, user_review=user_review)

@app.route('/book/<int:book_id>/review', methods=['GET', 'POST'])
@login_required
def add_review(book_id):
    book = Book.query.get_or_404(book_id)
    existing_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
    
    if existing_review:
        flash('Вы уже оставили рецензию на эту книгу.', 'warning')
        return redirect(url_for('view_book', book_id=book.id))

    if request.method == 'POST':
        try:
            rating = int(request.form.get('rating'))
            text = bleach.clean(request.form.get('text'))

            review = Review(book_id=book.id, user_id=current_user.id, rating=rating, text=text)
            db.session.add(review)
            db.session.commit()
            return redirect(url_for('view_book', book_id=book.id))
        except Exception:
            db.session.rollback()
            flash('Ошибка при сохранении рецензии.', 'danger')

    return render_template('review_form.html', book=book)

# Консольная утилита для инициализации БД демонстрационными данными
@app.cli.command("init-db")
def init_db():
    db.create_all()
    # Создание базовых ролей
    admin_role = Role(name='администратор', description='Полный доступ')
    moder_role = Role(name='модератор', description='Редактирование и модерация')
    user_role = Role(name='пользователь', description='Только рецензии')
    
    db.session.add_all([admin_role, moder_role, user_role])
    db.session.flush()

    # Администратор Скрынникова Полина Андреевна
    admin_user = User(
        login='admin', 
        password_hash=generate_password_hash('password'), 
        last_name='Скрынникова', 
        first_name='Полина', 
        middle_name='Андреевна', 
        role_id=admin_role.id
    )
    
    moder_user = User(login='moder', password_hash=generate_password_hash('password'), last_name='Петров', first_name='Петр', role_id=moder_role.id)
    regular_user = User(login='user', password_hash=generate_password_hash('password'), last_name='Сидоров', first_name='Сидор', role_id=user_role.id)
    
    # Демо-жанры
    g1 = Genre(name='Фантастика')
    g2 = Genre(name='Роман')
    g3 = Genre(name='Наука')

    db.session.add_all([admin_user, moder_user, regular_user, g1, g2, g3])
    db.session.commit()
    print("База данных успешно инициализирована базовыми ролями и юзерами!")

if __name__ == '__main__':
    app.run(debug=True)